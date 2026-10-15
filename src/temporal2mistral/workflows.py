"""Rewrite workflow classes, their handlers, child workflows and continue-as-new.

Temporal:
    @workflow.defn
    class GreetingWorkflow:
        @workflow.run
        async def run(self, name: str) -> str: ...

Mistral:
    @workflow.define(name="GreetingWorkflow")
    class GreetingWorkflow:
        @workflow.entrypoint
        async def run(self, name: str) -> str: ...
"""

from __future__ import annotations

import libcst as cst

from .calls import read_temporal_call
from .context import FileContext
from .durations import timedelta_code
from .scan import Workflow
from .signatures import check_signature
from .syntax import code_of, dotted_name, keyword_args, positional_args, split_decorator, string_value

# Temporal `execute_child_workflow` keyword -> Mistral `execute_workflow` keyword
CHILD_OPTIONS = {
    "id": "execution_id",
    "execution_timeout": "execution_timeout",
    "parent_close_policy": "parent_close_policy",
}


def rewrite_workflow_decorator(
    ctx: FileContext, cls: cst.ClassDef, original: cst.Decorator, updated: cst.Decorator
) -> cst.Decorator:
    """`@workflow.defn(...)` -> `@workflow.define(name=...)`."""
    ctx.changed = True
    ctx.need("mistralai.workflows", "workflow")
    class_name = cls.name.value
    _, call = split_decorator(updated)
    keywords = keyword_args(call)

    # Temporal names a workflow after its class by default; keep that name.
    if "name" in keywords:
        arguments = [f"name={code_of(keywords['name'].value)}"]
    else:
        arguments = [f'name="{class_name}"']
    for keyword, arg in keywords.items():
        if keyword == "name":
            continue
        if keyword == "sandboxed":
            arguments.append(f"enforce_determinism={code_of(arg.value)}")
        elif keyword == "dynamic":
            ctx.report(
                "T2M001", original, f"Dynamic workflow `{class_name}` is not supported", "Define one workflow per name."
            )
        else:
            ctx.report("T2M011", original, f"`@workflow.defn({keyword}=...)` dropped on `{class_name}`")

    if ctx.options.execution_timeout is not None:
        ctx.need("datetime", "timedelta")
        arguments.append(f"execution_timeout={timedelta_code(ctx.options.execution_timeout)}")
    else:
        ctx.report(
            "T2M010",
            original,
            f"Workflow `{class_name}` had no execution timeout on Temporal; Mistral cancels it after 1 hour by default",
            "Pass `execution_timeout=timedelta(...)` to `@workflow.define`, or rerun with `--execution-timeout 30d`.",
        )
    return updated.with_changes(decorator=cst.parse_expression(f"workflow.define({', '.join(arguments)})"))


def rewrite_workflow_method(
    ctx: FileContext,
    original: cst.FunctionDef,
    updated: cst.FunctionDef,
    class_name: str,
    update_handlers: set[str],
) -> cst.FunctionDef:
    """Rewrite the decorators of a method of a workflow class, then check its signature."""
    new_decorators = []
    kind = None  # "entrypoint", "signal", "query" or "update" when the method is one of those
    for original_decorator, decorator in zip(original.decorators, updated.decorators):
        target, _ = split_decorator(original_decorator)
        target_name = ctx.full_name(target)

        if target_name == "temporalio.workflow.run":
            ctx.need("mistralai.workflows", "workflow")
            decorator = decorator.with_changes(decorator=cst.parse_expression("workflow.entrypoint"))
            kind = "entrypoint"
        elif target_name in ("temporalio.workflow.signal", "temporalio.workflow.query", "temporalio.workflow.update"):
            kind = target_name.split(".")[-1]
            decorator = _handler_decorator(ctx, original, original_decorator, decorator, kind)
        elif target_name == "temporalio.workflow.init":
            ctx.report(
                "T2M012",
                original_decorator,
                f"`@workflow.init` on `{class_name}.{original.name.value}` is not supported",
                "Mistral instantiates workflow classes without arguments: move the initialisation into the entrypoint.",
            )
        elif isinstance(target, cst.Attribute) and target.attr.value == "validator":
            handler = dotted_name(target.value)
            if handler in update_handlers:
                ctx.report(
                    "T2M013",
                    original_decorator,
                    f"Validator `{original.name.value}` of update `{handler}` is not supported by Mistral",
                    "Validate inside the update handler and raise to reject the update.",
                )
        new_decorators.append(decorator)

    updated = updated.with_changes(decorators=new_decorators)
    if kind is not None:
        updated = check_signature(ctx, original, updated, kind=kind, is_method=True)
    return updated


def _handler_decorator(
    ctx: FileContext, method: cst.FunctionDef, original: cst.Decorator, updated: cst.Decorator, kind: str
) -> cst.Decorator:
    """`@workflow.signal` -> `@workflow.signal()`: Mistral's handler decorators always take parentheses."""
    ctx.need("mistralai.workflows", "workflow")
    _, call = split_decorator(updated)
    arguments = []
    if call is not None and positional_args(call):
        arguments.append(f"name={code_of(positional_args(call)[0].value)}")
    for keyword, arg in keyword_args(call).items():
        if keyword in ("name", "description"):
            arguments.append(f"{keyword}={code_of(arg.value)}")
        elif keyword == "dynamic":
            ctx.report(
                "T2M001",
                original,
                f"Dynamic {kind} handler `{method.name.value}` is not supported",
                "Declare one handler per name.",
            )
        else:
            ctx.report("T2M014", original, f"`@workflow.{kind}({keyword}=...)` dropped on `{method.name.value}`")
    return updated.with_changes(decorator=cst.parse_expression(f"workflow.{kind}({', '.join(arguments)})"))


def rewrite_child_workflow_call(
    ctx: FileContext, original: cst.Call, updated: cst.Call, api: str
) -> cst.BaseExpression:
    """`workflow.execute_child_workflow(Child.run, x, id=...)` ->
    `workflow.execute_workflow(Child, params=ChildParams(input=x), execution_id=...)`."""
    ctx.changed = True
    original_target = read_temporal_call(original, target_keyword="workflow").target
    temporal_call = read_temporal_call(updated, target_keyword="workflow")

    child, class_code = _child_workflow(ctx, original_target, temporal_call.target)
    if child is None or class_code is None:
        label = code_of(original_target) if original_target is not None else "?"
        ctx.report(
            "T2M041",
            original,
            f"Child workflow `{label}` could not be resolved to a workflow class of the project",
            "Call `workflow.execute_workflow(ChildWorkflowClass, params=<pydantic model>)` yourself.",
        )
        return updated

    params = workflow_params(ctx, original, child, temporal_call.payload, temporal_call.payload_is_unpacked)
    if params is None:
        return updated

    ctx.need("mistralai.workflows", "workflow")
    arguments = [class_code, f"params={params}"]
    for option, arg in temporal_call.options.items():
        if option in CHILD_OPTIONS:
            arguments.append(f"{CHILD_OPTIONS[option]}={code_of(arg.value)}")
        elif option != "result_type":
            ctx.report("T2M042", original, f"`{option}=` dropped from child workflow `{child.class_name}`")
    if api == "start_child_workflow":
        arguments.append("wait=False")
    ctx.report("T2M040", original, f"Child workflow `{child.class_name}` started with `workflow.execute_workflow`")
    return cst.parse_expression(f"workflow.execute_workflow({', '.join(arguments)})")


def _child_workflow(
    ctx: FileContext, original_target: cst.BaseExpression | None, updated_target: cst.BaseExpression | None
) -> tuple[Workflow | None, str | None]:
    """The child workflow and the code naming its class, for targets such as
    `Child.run`, `Child` or `"ChildWorkflowName"` (only when defined in this file)."""
    if original_target is None or updated_target is None:
        return None, None

    temporal_name = string_value(original_target)
    if temporal_name is not None:
        child = ctx.project.workflow_with_temporal_name(temporal_name)
        if child is not None and child.path == str(ctx.path):
            return child, child.class_name
        return None, None

    name = dotted_name(original_target)
    if name is None:
        return None, None
    parts = name.split(".")
    if len(parts) >= 2 and isinstance(updated_target, cst.Attribute):
        # `Child.run`: the class is everything before `.run`
        child = _find_workflow(ctx, parts[-2])
        if child is not None:
            return child, code_of(updated_target.value)
    child = _find_workflow(ctx, parts[-1])
    if child is not None:
        return child, code_of(updated_target)
    return None, None


def _find_workflow(ctx: FileContext, class_name: str) -> Workflow | None:
    """The workflow class defined in this file, or else the only one with that name in the project."""
    return ctx.project.workflow_defined_at(str(ctx.path), class_name) or ctx.project.workflow_named(class_name)


def rewrite_continue_as_new(
    ctx: FileContext, original: cst.Call, updated: cst.Call, current: Workflow | None
) -> cst.BaseExpression:
    """`workflow.continue_as_new(x)` -> `workflow.continue_as_new(MyWorkflowParams(arg=x))`."""
    ctx.changed = True
    extra = [keyword for keyword in keyword_args(updated) if keyword not in ("arg", "args")]
    if extra:
        ctx.report(
            "T2M050",
            original,
            f"`workflow.continue_as_new({', '.join(keyword + '=...' for keyword in extra)})` is not supported",
            "Mistral's `workflow.continue_as_new(params)` restarts the same workflow with new params only.",
        )
        return updated
    if current is None:
        ctx.report("T2M050", original, "`continue_as_new` outside a workflow class could not be converted")
        return updated

    temporal_call = read_temporal_call(updated, target_keyword=None)
    params = workflow_params(ctx, original, current, temporal_call.payload, temporal_call.payload_is_unpacked)
    if params is None:
        return updated
    ctx.need("mistralai.workflows", "workflow")
    return cst.parse_expression(f"workflow.continue_as_new({params})")


def workflow_params(
    ctx: FileContext, call: cst.Call, target: Workflow, payload: list[str], unpacked: bool
) -> str | None:
    """Mistral passes the input of a workflow as one pydantic model; build it from Temporal's arguments.

    If the workflow takes a single pydantic model, the argument is used as is.
    Otherwise a `<Workflow>Params` model is generated in this file (see params_model_code).
    """
    if unpacked:
        ctx.report(
            "T2M041",
            call,
            f"Arguments of `{target.class_name}` are unpacked from a sequence and cannot be mapped to its parameters",
            "Build the params model by hand.",
        )
        return None
    if len(payload) > len(target.parameters):
        ctx.report(
            "T2M041", call, f"`{target.class_name}` takes {len(target.parameters)} argument(s), {len(payload)} given"
        )
        return None

    single_parameter = len(target.parameters) == 1 and len(payload) == 1
    if single_parameter:
        annotation = target.parameters[0].annotation or ""
        if annotation.split(".")[-1] in ctx.project.pydantic_models:
            return payload[0]

    for parameter in target.parameters[len(payload) :]:
        if parameter.default is not None and not parameter.default_is_literal:
            ctx.report(
                "T2M043",
                call,
                f"`{target.class_name}` parameter `{parameter.name}` is omitted "
                f"and its default `{parameter.default}` is not a literal",
                f"Pass `{parameter.name}` explicitly.",
            )
    ctx.params_models[target.class_name] = target
    arguments = [f"{parameter.name}={value}" for parameter, value in zip(target.parameters, payload)]
    return f"{target.class_name}Params({', '.join(arguments)})"


def params_model_code(workflow: Workflow) -> str:
    """Source of the pydantic model used to start ``workflow`` as a child or to continue it as new.

    Fields are typed `Any`: the workflow itself validates its input against its own annotations.
    """
    lines = [
        f"class {workflow.class_name}Params(BaseModel):",
        f'    """Input of the `{workflow.name}` workflow (generated by temporal2mistral)."""',
    ]
    if workflow.parameters:
        lines.append("")
    for parameter in workflow.parameters:
        if parameter.default is not None and parameter.default_is_literal:
            lines.append(f"    {parameter.name}: Any = {parameter.default}")
        else:
            lines.append(f"    {parameter.name}: Any")
    return "\n".join(lines) + "\n"

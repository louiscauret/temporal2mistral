"""Convert code that starts and talks to workflows from the Temporal client to the Mistral API client.

Temporal:
    client = await Client.connect("localhost:7233")
    result = await client.execute_workflow(GreetingWorkflow.run, "World", id="hello", task_queue="q")

    handle = await client.start_workflow(GreetingWorkflow.run, "World", id="hello", task_queue="q")
    await handle.signal(GreetingWorkflow.approve, "Louis")
    status = await handle.query(GreetingWorkflow.status)
    result = await handle.result()

Mistral:
    client = Mistral(api_key=os.environ["MISTRAL_API_KEY"])
    result = await client.workflows.execute_workflow_and_wait_async(
        workflow_identifier="GreetingWorkflow", input={"name": "World"}, execution_id="hello"
    )

    handle = await client.workflows.execute_workflow_async(workflow_identifier=..., input=..., execution_id="hello")
    await client.workflows.executions.signal_workflow_execution_async(
        execution_id=handle.execution_id, name="approve", input={"approver": "Louis"}
    )
    status = (await client.workflows.executions.query_workflow_execution_async(...)).result
    result = (await client.workflows.wait_for_workflow_completion_async(handle.execution_id)).result

Inputs follow Mistral's rule: a single pydantic model parameter is the input itself, otherwise the
input is a dict of the parameters. A function is converted only when every use of its client is one
of the calls above; otherwise it is left as is and reported (T2M071).
"""

from __future__ import annotations

import json

import libcst as cst

from .calls import read_temporal_call
from .context import FileContext
from .scan import Handler, Parameter, Workflow
from .syntax import (
    code_of,
    dotted_name,
    keyword_args,
    names_used,
    single_assignment,
    statement_blocks,
    string_value,
)
from .workers import CLIENT_OPTIONS_WITH_BEHAVIOUR

CONNECT = "temporalio.client.Client.connect"
MISTRAL_CLIENT = 'Mistral(api_key=os.environ["MISTRAL_API_KEY"])'

# Options of `execute_workflow` / `start_workflow` that need nothing more on Mistral
# (`id` becomes `execution_id`, and Mistral routes executions to deployments instead of task queues)
START_OPTIONS = {"id", "task_queue", "result_type"}

# handle method -> (keyword naming the handler in Temporal's signature, Mistral method, has a result)
HANDLER_CALLS = {
    "signal": ("signal", "signal_workflow_execution_async", False),
    "query": ("query", "query_workflow_execution_async", True),
    "execute_update": ("update", "update_workflow_execution_async", True),
}


def plan_client_conversion(ctx: FileContext, function: cst.FunctionDef) -> None:
    """Record in ``ctx.replacements`` the conversion of every Temporal client created in ``function``."""
    for block in statement_blocks(function):
        for statement in block.body:
            assigned = single_assignment(statement)
            if assigned is None or not isinstance(assigned[1], cst.Await):
                continue
            connect = assigned[1].expression
            if not isinstance(connect, cst.Call) or ctx.full_name(connect.func) != CONNECT:
                continue
            if CLIENT_OPTIONS_WITH_BEHAVIOUR & set(keyword_args(connect)):
                continue  # e.g. a data converter or plugins: left to the user (T2M071)
            replacements = _plan(ctx, function, client=assigned[0], connect=assigned[1])
            if replacements is None:
                continue
            ctx.changed = True
            ctx.need("os")
            ctx.need("mistralai.client", "Mistral")
            ctx.replacements.update(replacements)
            ctx.converted_calls.add(connect)
            ctx.report(
                "T2M073",
                connect,
                f"Temporal client `{assigned[0]}` replaced by the Mistral API client",
                "Results come back as JSON values (dicts), not as instances of the workflow's return type. "
                "Set MISTRAL_API_KEY in the environment.",
            )


def _plan(ctx: FileContext, function: cst.FunctionDef, client: str, connect: cst.Await) -> dict | None:
    """The replacements for one client, or None when one of its uses cannot be converted."""
    replacements: dict[cst.CSTNode, str] = {connect: MISTRAL_CLIENT}
    handles: dict[str, Workflow] = {}  # handles returned by `start_workflow`, with their workflow

    for name in names_used(function, client):
        if isinstance(ctx.parents[name], cst.AssignTarget):
            continue  # `client = ...`
        call, method = _awaited_method_call(ctx, name)
        if call is None or method not in ("execute_workflow", "start_workflow"):
            return None
        started = _start(ctx, call)
        if started is None:
            return None
        workflow, arguments = started
        awaited = ctx.parents[call]
        if method == "execute_workflow":
            replacements[awaited] = f"await {client}.workflows.execute_workflow_and_wait_async({arguments})"
            continue
        handle = _assigned_name(ctx, awaited)  # `handle = await client.start_workflow(...)`
        if handle is None:
            return None
        handles[handle] = workflow
        replacements[awaited] = f"await {client}.workflows.execute_workflow_async({arguments})"

    for handle, workflow in handles.items():
        for name in names_used(function, handle):
            if isinstance(ctx.parents[name], cst.AssignTarget):
                continue
            replacement = _handle_use(ctx, client, handle, workflow, name)
            if replacement is None:
                return None
            node, code = replacement
            replacements[node] = code
    return replacements


def _handle_use(
    ctx: FileContext, client: str, handle: str, workflow: Workflow, name: cst.Name
) -> tuple[cst.CSTNode, str] | None:
    """The replacement of one use of a workflow handle: `.id`, `.result()`, `.signal(...)`..."""
    execution_id = f"execution_id={handle}.execution_id"
    attribute = ctx.parents[name]
    is_id = isinstance(attribute, cst.Attribute) and attribute.attr.value == "id"
    if is_id and not isinstance(ctx.parents[attribute], cst.Call):
        return attribute, f"{handle}.execution_id"

    call, method = _awaited_method_call(ctx, name)
    if call is None:
        return None
    awaited = ctx.parents[call]
    if method == "result" and not call.args:
        return awaited, f"(await {client}.workflows.wait_for_workflow_completion_async({handle}.execution_id)).result"
    if method not in HANDLER_CALLS:
        return None

    target_keyword, mistral_method, has_result = HANDLER_CALLS[method]
    temporal_call = read_temporal_call(call, target_keyword)
    handler = _handler(workflow, temporal_call.target)
    if handler is None or temporal_call.payload_is_unpacked or set(temporal_call.options) - {"result_type"}:
        return None
    arguments = [execution_id, f"name={json.dumps(handler.name)}"]
    input_code = _input(ctx, handler.parameters, temporal_call.payload)
    if input_code is None:
        return None
    if input_code:
        arguments.append(f"input={input_code}")
    code = f"await {client}.workflows.executions.{mistral_method}({', '.join(arguments)})"
    return awaited, f"({code}).result" if has_result else code


def _start(ctx: FileContext, call: cst.Call) -> tuple[Workflow, str] | None:
    """For `execute_workflow` / `start_workflow`: the workflow and the arguments of the Mistral call."""
    temporal_call = read_temporal_call(call, target_keyword="workflow")
    workflow = _workflow(ctx, temporal_call.target)
    if workflow is None or temporal_call.payload_is_unpacked or set(temporal_call.options) - START_OPTIONS:
        return None
    arguments = [f"workflow_identifier={json.dumps(workflow.name)}"]
    input_code = _input(ctx, workflow.parameters, temporal_call.payload)
    if input_code is None:
        return None
    if input_code:
        arguments.append(f"input={input_code}")
    if "id" in temporal_call.options:
        arguments.append(f"execution_id={code_of(temporal_call.options['id'].value)}")
    return workflow, ", ".join(arguments)


def _input(ctx: FileContext, parameters: list[Parameter], payload: list[str]) -> str | None:
    """The `input=` of a Mistral call ("" for none), or None when the arguments do not fit the parameters."""
    if len(payload) > len(parameters):
        return None
    if not payload:
        return ""
    annotation = parameters[0].annotation or ""
    if len(parameters) == 1 and annotation.split(".")[-1] in ctx.project.pydantic_models:
        return f'{payload[0]}.model_dump(mode="json")'
    items = [f"{json.dumps(parameter.name)}: {value}" for parameter, value in zip(parameters, payload)]
    return "{" + ", ".join(items) + "}"


def _workflow(ctx: FileContext, target: cst.BaseExpression | None) -> Workflow | None:
    """The workflow of `GreetingWorkflow.run`, `GreetingWorkflow` or `"GreetingWorkflow"`."""
    if target is None:
        return None
    name = string_value(target)
    if name is not None:
        return ctx.project.workflow_with_temporal_name(name)
    parts = (dotted_name(target) or "").split(".")
    if len(parts) >= 2 and ctx.project.workflow_named(parts[-2]) is not None:
        return ctx.project.workflow_named(parts[-2])
    return ctx.project.workflow_named(parts[-1])


def _handler(workflow: Workflow, target: cst.BaseExpression | None) -> Handler | None:
    """The handler of `GreetingWorkflow.approve` or `"approve"`."""
    if target is None:
        return None
    name = string_value(target)
    if name is not None:
        for handler in workflow.handlers.values():
            if handler.name == name:
                return handler
        return None
    method = (dotted_name(target) or "").split(".")[-1]
    return workflow.handlers.get(method)


def _awaited_method_call(ctx: FileContext, name: cst.Name) -> tuple[cst.Call | None, str | None]:
    """For `await name.method(...)`: the call and the method name; (None, None) for anything else."""
    attribute = ctx.parents[name]
    if not isinstance(attribute, cst.Attribute) or attribute.value is not name:
        return None, None
    call = ctx.parents[attribute]
    if not isinstance(call, cst.Call) or call.func is not attribute or not isinstance(ctx.parents[call], cst.Await):
        return None, None
    return call, attribute.attr.value


def _assigned_name(ctx: FileContext, value: cst.BaseExpression) -> str | None:
    """`name` in `name = value`."""
    assign = ctx.parents[value]
    if isinstance(assign, cst.Assign) and len(assign.targets) == 1 and isinstance(assign.targets[0].target, cst.Name):
        return assign.targets[0].target.value
    return None

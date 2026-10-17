"""Show, for each call of the Temporal client, the Mistral API client code to write instead.

Client code is not rewritten: it is usually spread across an application, and Mistral returns
results as JSON values instead of the workflow's return type. Each call gets a T2M071 diagnostic
whose hint is its translation:

    await client.start_workflow(GreetingWorkflow.run, "World", id="hello", task_queue="q")
    -> await client.workflows.execute_workflow_async(
           workflow_identifier="GreetingWorkflow", input={"name": "World"}, execution_id="hello")

    await handle.signal(GreetingWorkflow.approve, "Louis")
    -> await client.workflows.executions.signal_workflow_execution_async(
           execution_id=handle.execution_id, name="approve", input={"approver": "Louis"})

A call is recognised by what it targets: `execute_workflow` / `start_workflow` on a workflow of
the project, `signal` / `query` / `execute_update` on one of its handlers. Inputs follow Mistral's
rule: a single pydantic model parameter is the input itself, otherwise a dict of the parameters.
"""

from __future__ import annotations

import json

import libcst as cst

from .calls import read_temporal_call
from .context import FileContext
from .scan import Handler, Parameter, Workflow
from .syntax import code_of, dotted_name, string_value

README_SECTION = "https://github.com/louiscauret/temporal2mistral#migrating-client-code"

# handle method -> (keyword naming the handler in Temporal's signature, Mistral method, returns a result)
HANDLER_CALLS = {
    "signal": ("signal", "signal_workflow_execution_async", False),
    "query": ("query", "query_workflow_execution_async", True),
    "execute_update": ("update", "update_workflow_execution_async", True),
}


def report_client_connect(ctx: FileContext, call: cst.Call) -> None:
    """`Client.connect(...)`: the Mistral client replaces it."""
    ctx.report(
        "T2M071",
        call,
        "Temporal client in use: starting, signalling and querying workflows goes through the Mistral API",
        'Create `client = Mistral(api_key=os.environ["MISTRAL_API_KEY"])` (from mistralai.client import Mistral). '
        f"Each call to change is reported with its translation; see {README_SECTION}",
        once_per_file=True,
    )


def report_client_call(ctx: FileContext, call: cst.Call) -> None:
    """A call on a Temporal client or workflow handle: report it with the Mistral code to write."""
    if not isinstance(call.func, cst.Attribute):
        return
    method = call.func.attr.value
    receiver = code_of(call.func.value)
    if method in ("execute_workflow", "start_workflow"):
        translation = _start_translation(ctx, call, method)
    elif method in HANDLER_CALLS:
        translation = _handler_translation(ctx, call, receiver, method)
    else:
        return
    if translation is not None:
        ctx.report("T2M071", call, f"`{receiver}.{method}(...)` goes through the Mistral API client", translation)


def _start_translation(ctx: FileContext, call: cst.Call, method: str) -> str | None:
    temporal_call = read_temporal_call(call, target_keyword="workflow")
    workflow = _workflow(ctx, temporal_call.target)
    if workflow is None or temporal_call.payload_is_unpacked:
        return None
    input_argument = _input_argument(ctx, workflow.parameters, temporal_call.payload)
    if input_argument is None:
        return None
    arguments = [f"workflow_identifier={json.dumps(workflow.name)}", *input_argument]
    if "id" in temporal_call.options:
        arguments.append(f"execution_id={code_of(temporal_call.options['id'].value)}")

    if method == "execute_workflow":
        return f"`await client.workflows.execute_workflow_and_wait_async({', '.join(arguments)})`"
    return (
        f"`handle = await client.workflows.execute_workflow_async({', '.join(arguments)})`; "
        "then `await handle.result()` becomes "
        "`(await client.workflows.wait_for_workflow_completion_async(handle.execution_id)).result`"
    )


def _handler_translation(ctx: FileContext, call: cst.Call, handle: str, method: str) -> str | None:
    target_keyword, mistral_method, returns_result = HANDLER_CALLS[method]
    temporal_call = read_temporal_call(call, target_keyword)
    handler = _handler(ctx, temporal_call.target)
    if handler is None or temporal_call.payload_is_unpacked:
        return None
    input_argument = _input_argument(ctx, handler.parameters, temporal_call.payload)
    if input_argument is None:
        return None
    arguments = [f"execution_id={handle}.execution_id", f"name={json.dumps(handler.name)}", *input_argument]
    code = f"await client.workflows.executions.{mistral_method}({', '.join(arguments)})"
    if returns_result:
        code = f"({code}).result"
    return f"`{code}`"


def _input_argument(ctx: FileContext, parameters: list[Parameter], payload: list[str]) -> list[str] | None:
    """`["input=..."]`, `[]` without arguments, or None when the arguments do not fit the parameters."""
    if len(payload) > len(parameters):
        return None
    if not payload:
        return []
    annotation = parameters[0].annotation or ""
    if len(parameters) == 1 and annotation.split(".")[-1] in ctx.project.pydantic_models:
        return [f'input={payload[0]}.model_dump(mode="json")']
    items = [f"{json.dumps(parameter.name)}: {value}" for parameter, value in zip(parameters, payload)]
    return ["input={" + ", ".join(items) + "}"]


def _workflow(ctx: FileContext, target: cst.BaseExpression | None) -> Workflow | None:
    """The workflow of `GreetingWorkflow.run`, `GreetingWorkflow` or `"GreetingWorkflow"`."""
    if target is None:
        return None
    name = string_value(target)
    if name is not None:
        return ctx.project.workflow_with_temporal_name(name)
    parts = (dotted_name(target) or "").split(".")
    if len(parts) >= 2:
        workflow = ctx.project.workflow_named(parts[-2])  # `GreetingWorkflow.run`
        if workflow is not None:
            return workflow
    return ctx.project.workflow_named(parts[-1])


def _handler(ctx: FileContext, target: cst.BaseExpression | None) -> Handler | None:
    """The handler of `GreetingWorkflow.approve`, or of `"approve"` when one workflow has it."""
    if target is None:
        return None
    name = string_value(target)
    if name is not None:
        found = []
        for workflow in ctx.project.workflows:
            for handler in workflow.handlers.values():
                if handler.name == name:
                    found.append(handler)
        return found[0] if len(found) == 1 else None
    parts = (dotted_name(target) or "").split(".")
    workflow = ctx.project.workflow_named(parts[-2]) if len(parts) >= 2 else None
    return workflow.handlers.get(parts[-1]) if workflow is not None else None

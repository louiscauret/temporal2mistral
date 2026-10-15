"""Rewrite activity definitions and the calls that run them.

Temporal:
    @activity.defn
    def compose_greeting(input: ComposeGreetingInput) -> str: ...

    await workflow.execute_activity(compose_greeting, x, start_to_close_timeout=timedelta(seconds=10))

Mistral:
    @activity(start_to_close_timeout=timedelta(seconds=10))
    async def compose_greeting(input: ComposeGreetingInput) -> str: ...

    await compose_greeting(x)
"""

from __future__ import annotations

import libcst as cst

from .activity_options import decorator_options
from .calls import read_temporal_call
from .context import FileContext
from .signatures import check_signature
from .syntax import code_of, keyword_args, positional_args, split_decorator, string_value

# Call options that activity_options.py moves to the decorator, or that need no replacement
MOVED_OR_IGNORED_OPTIONS = {
    "start_to_close_timeout",
    "schedule_to_close_timeout",
    "heartbeat_timeout",
    "retry_policy",
    "result_type",
}


def rewrite_activity_definition(
    ctx: FileContext,
    original: cst.FunctionDef,
    updated: cst.FunctionDef,
    decorator_index: int,
    is_method: bool,
    is_nested: bool,
) -> cst.FunctionDef | cst.FlattenSentinel:
    ctx.changed = True
    name = original.name.value
    original_decorator = original.decorators[decorator_index]

    if is_method:
        ctx.report(
            "T2M030",
            original_decorator,
            f"Activity method `{name}` is defined on a class",
            "Mistral activities are module-level `async def` functions: move it out of the class and inject "
            "shared clients with `Depends(...)` (mistralai.workflows.Depends).",
        )
        return updated
    if is_nested:
        ctx.report(
            "T2M036",
            original_decorator,
            f"Activity `{name}` is defined inside a function",
            "Mistral activities are module-level `async def` functions: move it to the top level of the module "
            "and pass what it used from the enclosing function as parameters.",
        )
        return updated

    _, decorator_call = split_decorator(original_decorator)
    arguments = []
    keywords = keyword_args(decorator_call)
    if "name" in keywords:
        temporal_name = keywords["name"].value
    elif decorator_call is not None and positional_args(decorator_call):
        temporal_name = positional_args(decorator_call)[0].value
    else:
        temporal_name = None
    if temporal_name is not None and string_value(temporal_name) != name:
        arguments.append(f"name={code_of(temporal_name)}")
    for keyword in keywords:
        if keyword == "dynamic":
            ctx.report(
                "T2M001",
                original_decorator,
                f"Dynamic activity `{name}` is not supported",
                "Declare one activity per name.",
            )
        elif keyword != "name":
            ctx.report("T2M023", original_decorator, f"`@activity.defn({keyword}=...)` dropped on `{name}`")

    activity = ctx.project.activity_defined_at(str(ctx.path), name)
    calls = activity.calls if activity is not None else []
    options = decorator_options(name, calls, ctx.options.keep_unlimited_retries)
    for note in options.notes:
        ctx.report(note.code, original, note.message, note.hint)
    if options.uses_timedelta:
        ctx.need("datetime", "timedelta")
    arguments += options.arguments

    ctx.need("mistralai.workflows", "activity")
    new_decorator = cst.parse_expression(f"activity({', '.join(arguments)})")
    decorators = list(updated.decorators)
    decorators[decorator_index] = decorators[decorator_index].with_changes(decorator=new_decorator)
    updated = updated.with_changes(decorators=decorators)
    updated = check_signature(ctx, original, updated, kind="activity", is_method=False)

    if original.asynchronous is not None:
        return updated
    if ctx.options.sync_activities == "async":
        ctx.report(
            "T2M031",
            original,
            f"Synchronous activity `{name}` turned into `async def`",
            "Blocking calls (time.sleep, requests, ...) now block the worker's event loop: "
            "make them asynchronous or wrap them with `await asyncio.to_thread(...)`.",
        )
        return updated.with_changes(asynchronous=cst.Asynchronous())
    return _run_in_thread(ctx, original, updated)


def _run_in_thread(ctx: FileContext, original: cst.FunctionDef, updated: cst.FunctionDef) -> cst.FlattenSentinel:
    """Mistral activities must be `async def`. Keep the original body in a plain function and
    call it with `asyncio.to_thread`, like Temporal's `activity_executor` did:

        def _compose_greeting_sync(input: X) -> str: <original body>

        @activity(...)
        async def compose_greeting(input: X) -> str:
            return await asyncio.to_thread(_compose_greeting_sync, input)
    """
    name = original.name.value
    sync_name = f"_{name}_sync"
    sync_function = updated.with_changes(name=cst.Name(sync_name), decorators=[])

    params = original.params
    call_arguments = [sync_name]
    for param in list(params.posonly_params) + list(params.params):
        call_arguments.append(param.name.value)
    if isinstance(params.star_arg, cst.Param):
        call_arguments.append("*" + params.star_arg.name.value)
    for param in params.kwonly_params:
        call_arguments.append(f"{param.name.value}={param.name.value}")
    if params.star_kwarg is not None:
        call_arguments.append("**" + params.star_kwarg.name.value)

    returns = f" -> {code_of(updated.returns.annotation)}" if updated.returns is not None else ""
    async_function = cst.parse_statement(
        f"async def {name}({code_of(params)}){returns}:\n"
        f"    return await asyncio.to_thread({', '.join(call_arguments)})\n"
    )
    async_function = async_function.with_changes(
        decorators=updated.decorators,
        leading_lines=[cst.EmptyLine(), cst.EmptyLine()],
    )
    ctx.need("asyncio")

    hint = "The original body now runs in a thread through `asyncio.to_thread`, like Temporal's `activity_executor`."
    if "heartbeat" in code_of(original.body):
        hint += (
            " It heartbeats from that thread: check heartbeats still reach the server, or make the activity `async`."
        )
    ctx.report(
        "T2M031", original, f"Synchronous activity `{name}` wrapped in an `async def` that runs it in a thread", hint
    )
    return cst.FlattenSentinel([sync_function, async_function])


def rewrite_activity_call(ctx: FileContext, original: cst.Call, updated: cst.Call, api: str) -> cst.BaseExpression:
    """`workflow.execute_activity(fn, x, ...)` -> `fn(x)`. ``api`` is e.g. "execute_activity"."""
    ctx.changed = True
    temporal_call = read_temporal_call(updated, target_keyword="activity")
    original_target = read_temporal_call(original, target_keyword="activity").target
    if original_target is None or temporal_call.target is None:
        ctx.report("T2M032", original, f"`workflow.{api}` without an activity argument")
        return updated

    callee = _callee(ctx, original, original_target, temporal_call.target)
    if callee is None:
        return updated

    target_code = code_of(original_target)
    for option in temporal_call.options:
        if option in MOVED_OR_IGNORED_OPTIONS:
            continue
        if option == "task_queue":
            hint = "Use `rate_limit=` or `sticky_to_worker=` on `@activity` to control where activities run."
        else:
            hint = "Mistral activities take no per-call options."
        ctx.report("T2M023", original, f"`{option}=` dropped from the call to `{target_code}`", hint)

    new_call = f"{callee}({', '.join(temporal_call.payload)})"
    if api.startswith("start_"):
        ctx.need("asyncio")
        ctx.report(
            "T2M033",
            original,
            f"`workflow.{api}({target_code}, ...)` became `asyncio.create_task(...)`",
            "The handle is now an `asyncio.Task`: `.cancel()` and `await` keep working.",
        )
        new_call = f"asyncio.create_task({new_call})"
    if "local" in api:
        ctx.report(
            "T2M034",
            original,
            f"Local activity `{target_code}` is now a regular activity",
            "Mistral runs activities locally inside `run_activities_locally()` blocks.",
            once_per_file=True,
        )
    return cst.parse_expression(new_call)


def _callee(
    ctx: FileContext, call: cst.Call, original_target: cst.BaseExpression, updated_target: cst.BaseExpression
) -> str | None:
    """The code to call the activity with, or None (and a diagnostic) when the call must stay as is."""
    path = str(ctx.path)
    target_code = code_of(original_target)

    activity_name = string_value(original_target)
    if activity_name is not None:
        # `execute_activity("say-hello", ...)`: only convertible when the activity is in this file.
        found = ctx.project.find_called_activities(activity_name, path, by_activity_name=True)
        in_this_file = [a for a in found if a.path == path and not a.is_method]
        if len(in_this_file) == 1:
            return in_this_file[0].function
        defined_in = f" (defined in {found[0].module})" if found else ""
        ctx.report(
            "T2M032",
            call,
            f"Activity called by name `{activity_name}`{defined_in}",
            "Mistral activities are called directly: import the activity function and call it.",
        )
        return None

    target_name = ctx.full_name(original_target)
    found = ctx.project.find_called_activities(target_name, path) if target_name else []
    if any(a.is_method for a in found):
        ctx.report(
            "T2M030",
            call,
            f"`{target_code}` is a class-based activity",
            "Move it to a module-level `async def` decorated with `@activity(...)`.",
        )
        return None
    if any(a.is_nested for a in found):
        ctx.report(
            "T2M036",
            call,
            f"`{target_code}` is an activity defined inside a function",
            "Move it to the top level of the module.",
        )
        return None
    if found:
        return code_of(updated_target)

    is_local_variable = isinstance(original_target, cst.Name) and original_target.value not in ctx.imports
    if is_local_variable:
        # e.g. `fn = order_apples if ... else order_bananas`, then `execute_activity(fn, ...)`
        moved = [option for option in keyword_args(call) if option in MOVED_OR_IGNORED_OPTIONS - {"result_type"}]
        options_text = ", ".join(f"`{option}`" for option in moved) or "its options"
        ctx.report(
            "T2M035",
            call,
            f"`{target_code}` is not a known activity (chosen at runtime?): the call was converted but "
            f"{options_text} could not be attached to an activity",
            "Set those options on the `@activity(...)` decorator of every activity it can hold.",
        )
        return code_of(updated_target)

    ctx.report(
        "T2M032",
        call,
        f"`{target_code}` is not an activity defined in the converted project",
        "If it comes from another package, convert that package too or wrap it in a Mistral `@activity`.",
    )
    return None

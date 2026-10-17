"""Replace simple Temporal worker scripts by `run_worker`.

Temporal:
    client = await Client.connect("localhost:7233")
    worker = Worker(client, task_queue="hello", workflows=[GreetingWorkflow], activities=[compose_greeting])
    print("Worker started")
    await worker.run()

Mistral (activities register themselves, and the worker reads its settings from the environment):
    print("Worker started")
    await run_worker([GreetingWorkflow])

Two shapes are converted. Both just run the worker until it is stopped:

1. `worker = Worker(...)`, then log lines, then `await worker.run()`
2. `async with Worker(...):` whose body is log lines and `await interrupt_event.wait()`

"Log lines" are statements made of one call that is not awaited, such as `print(...)`.

The worker must also be the only user of a client created in the same function by
`client = await Client.connect(...)`, because that line goes too. Anything else is reported (T2M070).
"""

from __future__ import annotations

import libcst as cst

from .context import FileContext
from .syntax import (
    code_of,
    keyword_args,
    names_used,
    positional_args,
    single_assignment,
    single_expression,
    statement_blocks,
)

WORKER = "temporalio.worker.Worker"
CONNECT = "temporalio.client.Client.connect"

# Worker options that need nothing on Mistral: activities register themselves, synchronous
# activities run in a thread (see activities.py), and deployments replace task queues.
OPTIONS_NOT_NEEDED = {"activities", "activity_executor", "task_queue"}
# Options of the client that change how the worker behaves: the user has to port them.
CLIENT_OPTIONS_WITH_BEHAVIOUR = {"data_converter", "plugins", "interceptors", "runtime"}


def plan_worker_conversions(ctx: FileContext, function: cst.FunctionDef) -> None:
    """Record in ``ctx.replacements`` the new code of the worker scripts of ``function``."""
    for block in statement_blocks(function):
        statements = list(block.body)
        for index, statement in enumerate(statements):
            if isinstance(statement, cst.With):
                _plan_async_with(ctx, function, statement)
            else:
                _plan_worker_run(ctx, function, statement, statements[index + 1 :])


def _plan_worker_run(
    ctx: FileContext, function: cst.FunctionDef, statement: cst.BaseStatement, following: list[cst.BaseStatement]
) -> None:
    """Shape 1: `worker = Worker(...)`, log lines, `await worker.run()`."""
    assigned = single_assignment(statement)
    if assigned is None:
        return
    name, worker = assigned
    if not _is_convertible_worker(ctx, worker) or not _used_once(function, name):
        return
    for line in following:
        if _is_run_of(line, name):
            _convert(ctx, function, worker, {statement: "", line: _run_worker(worker)})
            return
        if not _is_log_line(line):
            return


def _plan_async_with(ctx: FileContext, function: cst.FunctionDef, statement: cst.With) -> None:
    """Shape 2: `async with Worker(...):` whose body is log lines and one wait that never ends."""
    if statement.asynchronous is None or len(statement.items) != 1 or statement.items[0].asname is not None:
        return
    worker = statement.items[0].item
    if not _is_convertible_worker(ctx, worker):
        return
    new_code = ""
    waits = 0
    for line in statement.body.body:
        if _is_wait_forever(ctx, line):
            new_code += _run_worker(worker)
            waits += 1
        elif _is_log_line(line):
            new_code += code_of(line)
        else:
            return
    if waits == 1:
        _convert(ctx, function, worker, {statement: new_code})


def _is_convertible_worker(ctx: FileContext, expression: cst.BaseExpression) -> bool:
    """`Worker(client, workflows=[...], ...)` without any option that matters on Mistral."""
    if not isinstance(expression, cst.Call) or ctx.full_name(expression.func) != WORKER:
        return False
    keywords = keyword_args(expression)
    if "workflows" not in keywords or len(positional_args(expression)) != 1:
        return False  # an activity-only worker, or options passed positionally
    for option in keywords:
        if option != "workflows" and option not in OPTIONS_NOT_NEEDED:
            return False  # interceptors, tuning...: the user has to decide
    return True


def _convert(
    ctx: FileContext, function: cst.FunctionDef, worker: cst.Call, replacements: dict[cst.CSTNode, str]
) -> None:
    """Replace the worker, and the line that connects its client."""
    connection = _client_connection(ctx, function, worker)
    if connection is None:
        return  # the client comes from elsewhere, is used elsewhere or has options: left to the user (T2M070)
    replacements[connection] = ""
    ctx.changed = True
    ctx.need("mistralai.workflows", "run_worker")
    ctx.replacements.update(replacements)
    hint = "Activities register themselves; the worker reads its settings (MISTRAL_API_KEY...) from the environment."
    if "task_queue" in keyword_args(worker):
        hint += " `task_queue` was dropped: Mistral routes executions to deployments."
    ctx.report("T2M072", worker, f"`Worker(...)` replaced by `{_run_worker(worker).strip()}`", hint)


def _client_connection(ctx: FileContext, function: cst.FunctionDef, worker: cst.Call) -> cst.BaseStatement | None:
    """The line `client = await Client.connect(...)` that creates the worker's client, when the worker
    is the only user of that client and the connection has no option that matters on Mistral."""
    client = positional_args(worker)[0].value
    if not isinstance(client, cst.Name) or not _used_once(function, client.value):
        return None
    for block in statement_blocks(function):
        for statement in block.body:
            assigned = single_assignment(statement)
            if assigned is None or assigned[0] != client.value or not isinstance(assigned[1], cst.Await):
                continue
            connect = assigned[1].expression
            if not isinstance(connect, cst.Call) or ctx.full_name(connect.func) != CONNECT:
                continue
            if CLIENT_OPTIONS_WITH_BEHAVIOUR & set(keyword_args(connect)):
                return None  # e.g. a data converter or plugins
            return statement
    return None


def _run_worker(worker: cst.Call) -> str:
    """The statement replacing the worker: `await run_worker([GreetingWorkflow])`."""
    return f"await run_worker({code_of(keyword_args(worker)['workflows'].value)})\n"


def _used_once(function: cst.FunctionDef, name: str) -> bool:
    """Is the variable ``name`` assigned, then used only once, in ``function``?"""
    return len(names_used(function, name)) == 2  # the assignment and the use


def _is_run_of(statement: cst.BaseStatement, name: str) -> bool:
    """`await <name>.run()`"""
    expression = single_expression(statement)
    if not isinstance(expression, cst.Await) or not isinstance(expression.expression, cst.Call):
        return False
    call = expression.expression
    return (
        not call.args
        and isinstance(call.func, cst.Attribute)
        and call.func.attr.value == "run"
        and isinstance(call.func.value, cst.Name)
        and call.func.value.value == name
    )


def _is_log_line(statement: cst.BaseStatement) -> bool:
    """A statement made of one call that is not awaited, such as `print(...)`."""
    return isinstance(single_expression(statement), cst.Call)


def _is_wait_forever(ctx: FileContext, statement: cst.BaseStatement) -> bool:
    """`await interrupt_event.wait()` (also as `_ = await ...`) or `await asyncio.Future()`."""
    expression = single_expression(statement)
    if expression is None:
        assigned = single_assignment(statement)
        expression = assigned[1] if assigned is not None else None
    if not isinstance(expression, cst.Await) or not isinstance(expression.expression, cst.Call):
        return False
    call = expression.expression
    if call.args:
        return False
    if isinstance(call.func, cst.Attribute) and call.func.attr.value == "wait":
        return True
    return ctx.full_name(call.func) == "asyncio.Future"

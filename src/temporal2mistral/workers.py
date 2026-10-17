"""Replace simple Temporal worker entry points by `run_worker`.

Temporal:
    client = await Client.connect("localhost:7233")
    worker = Worker(client, task_queue="hello", workflows=[GreetingWorkflow], activities=[compose_greeting])
    await worker.run()

Mistral (activities register themselves, and the worker connects with the settings of the environment):
    await run_worker([GreetingWorkflow])

Three shapes are converted, because they all just run the worker until it is stopped:

- `await Worker(...).run()`
- `worker = Worker(...)` followed by `await worker.run()`, with only log lines in between
- `async with Worker(...):` whose body only logs and waits forever (`await interrupt_event.wait()`)

"Log lines" are statements made of a single call that is not awaited, such as `print(...)`.

Anything else, such as a worker running while the same function starts workflows, is reported (T2M070).
"""

from __future__ import annotations

from dataclasses import dataclass

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
# Options of the worker's client that change its behaviour: the user has to port them.
CLIENT_OPTIONS_WITH_BEHAVIOUR = {"data_converter", "plugins", "interceptors", "runtime"}


@dataclass
class _WorkerCall:
    call: cst.Call
    run_worker: str  # the replacing statement, e.g. `await run_worker([GreetingWorkflow])`


def plan_worker_conversions(ctx: FileContext, function: cst.FunctionDef) -> None:
    """Find the worker entry points of ``function`` and record their replacements in ``ctx.replacements``."""
    for block in statement_blocks(function):
        statements = list(block.body)
        for index in range(len(statements)):
            _plan_statement(ctx, function, statements, index)


def _plan_statement(ctx: FileContext, function: cst.FunctionDef, statements: list, index: int) -> None:
    statement = statements[index]
    if isinstance(statement, cst.With):
        _plan_waiting_block(ctx, function, statement)
        return

    expression = single_expression(statement)
    # `await Worker(...).run()`
    if isinstance(expression, cst.Await) and _is_run_call(expression.expression):
        worker = _worker_call(ctx, expression.expression.func.value)
        if worker is not None:
            _convert(ctx, function, worker, {statement: worker.run_worker})
        return

    # `worker = Worker(...)`, log lines, then `await worker.run()`
    assigned = single_assignment(statement)
    worker = _worker_call(ctx, assigned[1]) if assigned is not None else None
    if worker is None or len(names_used(function, assigned[0])) != 2:  # the assignment and `.run()`
        return
    for following in statements[index + 1 :]:
        run = single_expression(following)
        is_run = (
            isinstance(run, cst.Await)
            and _is_run_call(run.expression)
            and isinstance(run.expression.func.value, cst.Name)
            and run.expression.func.value.value == assigned[0]
        )
        if is_run:
            _convert(ctx, function, worker, {statement: "", following: worker.run_worker})
            return
        if not _is_log_line(following):
            return


def _plan_waiting_block(ctx: FileContext, function: cst.FunctionDef, statement: cst.With) -> None:
    """`async with Worker(...):` whose body is log lines and one wait that never ends."""
    if statement.asynchronous is None or len(statement.items) != 1 or statement.items[0].asname is not None:
        return
    worker = _worker_call(ctx, statement.items[0].item)
    if worker is None:
        return
    new_code = ""
    waits = 0
    for line in statement.body.body:
        if _is_wait_forever_line(ctx, line):
            new_code += worker.run_worker
            waits += 1
        elif _is_log_line(line):
            new_code += code_of(line)
        else:
            return
    if waits == 1:
        _convert(ctx, function, worker, {statement: new_code})


def _is_log_line(statement: cst.BaseStatement) -> bool:
    return isinstance(single_expression(statement), cst.Call)


def _is_wait_forever_line(ctx: FileContext, statement: cst.BaseStatement) -> bool:
    """`await interrupt_event.wait()`, also as `_ = await interrupt_event.wait()`."""
    waited = single_expression(statement)
    if waited is None:
        assigned = single_assignment(statement)
        waited = assigned[1] if assigned is not None else None
    return isinstance(waited, cst.Await) and _waits_forever(ctx, waited.expression)


def _waits_forever(ctx: FileContext, expression: cst.BaseExpression) -> bool:
    """`interrupt_event.wait()`, `asyncio.Event().wait()` or `asyncio.Future()`."""
    if not isinstance(expression, cst.Call) or expression.args:
        return False
    if isinstance(expression.func, cst.Attribute) and expression.func.attr.value == "wait":
        return True
    return ctx.full_name(expression.func) == "asyncio.Future"


def _worker_call(ctx: FileContext, expression: cst.BaseExpression) -> _WorkerCall | None:
    """`Worker(client, workflows=[...], ...)` when it can become `run_worker([...])`."""
    if not isinstance(expression, cst.Call) or ctx.full_name(expression.func) != WORKER:
        return None
    keywords = keyword_args(expression)
    if "workflows" not in keywords or len(positional_args(expression)) > 1:
        return None  # an activity-only worker, or options passed positionally
    for option in keywords:
        if option != "workflows" and option not in OPTIONS_NOT_NEEDED:
            return None  # interceptors, tuning...: the user has to decide
    return _WorkerCall(expression, f"await run_worker({code_of(keywords['workflows'].value)})\n")


def _convert(
    ctx: FileContext, function: cst.FunctionDef, worker: _WorkerCall, replacements: dict[cst.CSTNode, str]
) -> None:
    connect = _client_connect(ctx, function, worker.call)
    if connect is not None and CLIENT_OPTIONS_WITH_BEHAVIOUR & set(keyword_args(connect[1])):
        return  # e.g. a data converter or plugins on the client: left to the user (T2M070)

    ctx.changed = True
    ctx.need("mistralai.workflows", "run_worker")
    ctx.replacements.update(replacements)
    ctx.converted_calls.add(worker.call)
    hint = "Activities register themselves; the worker reads its settings (MISTRAL_API_KEY...) from the environment."
    if "task_queue" in keyword_args(worker.call):
        hint += " `task_queue` was dropped: Mistral routes executions to deployments."
    ctx.report("T2M072", worker.call, f"`Worker(...)` replaced by `{worker.run_worker.strip()}`", hint)

    # The connection to Temporal goes too when the worker was the only one using it.
    if connect is not None:
        statement, connect_call, client = connect
        if len(names_used(function, client)) == 2:  # the assignment and the Worker argument
            ctx.replacements[statement] = ""
            ctx.converted_calls.add(connect_call)


def _client_connect(
    ctx: FileContext, function: cst.FunctionDef, worker_call: cst.Call
) -> tuple[cst.BaseStatement, cst.Call, str] | None:
    """`client = await Client.connect(...)` in ``function`` for the client of the worker, as
    (statement, connect call, client variable)."""
    arguments = positional_args(worker_call)
    if not arguments or not isinstance(arguments[0].value, cst.Name):
        return None
    client = arguments[0].value.value
    for block in statement_blocks(function):
        for statement in block.body:
            assigned = single_assignment(statement)
            if assigned is None or assigned[0] != client or not isinstance(assigned[1], cst.Await):
                continue
            connect = assigned[1].expression
            if isinstance(connect, cst.Call) and ctx.full_name(connect.func) == CONNECT:
                return statement, connect, client
    return None


def _is_run_call(expression: cst.BaseExpression) -> bool:
    """`<something>.run()`"""
    return (
        isinstance(expression, cst.Call)
        and not expression.args
        and isinstance(expression.func, cst.Attribute)
        and expression.func.attr.value == "run"
    )

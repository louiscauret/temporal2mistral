# temporal2mistral

[![CI](https://github.com/louiscauret/temporal2mistral/actions/workflows/ci.yml/badge.svg)](https://github.com/louiscauret/temporal2mistral/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/temporal2mistral?color=3776AB)](https://pypi.org/project/temporal2mistral/)
![Python](https://img.shields.io/badge/python-3.12%20|%203.13%20|%203.14-3776AB?logo=python&logoColor=white)
![Temporal](https://img.shields.io/badge/temporalio-1.29-141414?logo=temporal&logoColor=white)
![Mistral Workflows](https://img.shields.io/badge/mistralai--workflows-3.15-FA520F?logo=mistralai&logoColor=white)
![LibCST](https://img.shields.io/badge/built%20with-LibCST-4B8BBE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
![Status](https://img.shields.io/badge/status-alpha-orange)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Move Python code from the [Temporal SDK](https://github.com/temporalio/sdk-python) to
[Mistral Workflows](https://docs.mistral.ai/studio/workflows/getting-started/overview).

It rewrites what can be rewritten safely and gives you a report of the rest, file by file and line by line.

```console
$ temporal2mistral check ./src           # report only, writes nothing
$ temporal2mistral apply ./src --diff    # show the rewrite
$ temporal2mistral apply ./src           # rewrite in place (commit first)
```

Alpha. Tested against `temporalio` 1.29 and `mistralai-workflows` 3.15. Mistral Workflows is in public
preview, so its API can still move. This project is not affiliated with Temporal Technologies or Mistral AI.

## Why not search and replace?

Mistral Workflows runs on Temporal, so most of the API maps one to one. A few things don't:

- Temporal sets activity timeouts and retries at each call
  (`workflow.execute_activity(fn, x, start_to_close_timeout=...)`). Mistral sets them once on the
  definition (`@activity(start_to_close_timeout=...)`) and you call the activity directly
  (`await fn(x)`). The call is usually in a different module from the definition, so the tool scans
  the whole project before rewriting anything.
- Mistral builds a JSON schema from every signature. Parameters and return types must be annotated,
  activities must be module-level `async def` functions, query and update handlers must return
  something, and child workflows and `continue_as_new` take a single pydantic model.
- The defaults differ. A Mistral workflow is cancelled after 1 hour unless you set
  `execution_timeout` (Temporal has no limit), and activities retry 3 times (Temporal retries forever).

## Install

```console
pip install temporal2mistral
pip install "temporal2mistral[mistral]"      # with the Mistral SDK, for --import-check
```

Needs Python 3.12 or later, like the Mistral SDK. From a clone: `pip install -e ".[test,mistral]"`.

## Example

Before:

```python
@activity.defn
def compose_greeting(input: ComposeGreetingInput) -> str:
    activity.logger.info("Running activity with parameter %s" % input)
    return f"{input.greeting}, {input.name}!"


@workflow.defn
class GreetingWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        workflow.logger.info("Running workflow with parameter %s" % name)
        return await workflow.execute_activity(
            compose_greeting,
            ComposeGreetingInput("Hello", name),
            start_to_close_timeout=timedelta(seconds=10),
        )
```

After `temporal2mistral apply`:

```python
def _compose_greeting_sync(input: ComposeGreetingInput) -> str:
    temporal_activity.logger.info("Running activity with parameter %s" % input)
    return f"{input.greeting}, {input.name}!"


@activity(start_to_close_timeout=timedelta(seconds=10), retry_policy_max_attempts=0)
async def compose_greeting(input: ComposeGreetingInput) -> str:
    return await asyncio.to_thread(_compose_greeting_sync, input)


@workflow.define(name="GreetingWorkflow")
class GreetingWorkflow:
    @workflow.entrypoint
    async def run(self, name: str) -> str:
        temporal_workflow.logger.info("Running workflow with parameter %s" % name)
        return await compose_greeting(ComposeGreetingInput("Hello", name))
```

And `temporal2mistral check hello.py` prints:

```text
hello.py
    14  WARNING T2M031  Synchronous activity `compose_greeting` wrapped in an `async def` that runs it in a thread
               -> The original body now runs in a thread through `asyncio.to_thread`, like Temporal's `activity_executor`.
    14  INFO    T2M020  start_to_close_timeout, retry_policy_max_attempts moved to `@activity` on `compose_greeting` (from hello.py:24)
    14  INFO    T2M026  `retry_policy_max_attempts=0` (unlimited, Temporal's default) set on `compose_greeting`
               -> Remove it to use Mistral's default of 3 attempts, or rerun with --mistral-retry-defaults.
    15  INFO    T2M060  `activity.logger` is used through `temporal_activity` (Mistral activities run on Temporal)
    19  WARNING T2M010  Workflow `GreetingWorkflow` had no execution timeout on Temporal; Mistral cancels it after 1 hour by default
               -> Pass `execution_timeout=timedelta(...)` to `@workflow.define`, or rerun with `--execution-timeout 30d`.
    23  INFO    T2M060  `workflow.logger` is used through `temporal_workflow` (Mistral workflows run on Temporal)
               -> Works under a Mistral worker; raises in the SDK's local mode (execute_workflow outside a worker).

1 file(s) converted out of 1 using temporalio (1 scanned): 0 error(s), 2 warning(s), 4 info(s).
```

The output is correct but the imports aren't sorted, so run ruff, black or isort on it afterwards.

## What gets converted

| Temporal | Mistral |
|---|---|
| `@workflow.defn`, `@workflow.defn(name=..., sandboxed=False)` | `@workflow.define(name=..., enforce_determinism=False)`. The name defaults to the class name, as on Temporal |
| `@workflow.run` | `@workflow.entrypoint` |
| `@workflow.signal`, `@workflow.query`, `@workflow.update` (with `name=`, `description=`) | `@workflow.signal()`, `@workflow.query()`, `@workflow.update()` |
| `workflow.wait_condition`, `sleep`, `now`, `uuid4`, `random`, `unsafe.imports_passed_through` | same names on `mistralai.workflows.workflow` |
| `workflow.unsafe.sandbox_unrestricted()` | `workflow.unsafe.skip_determinism_enforcement()` |
| `@activity.defn`, `@activity.defn(name=...)` | `@activity(name=..., start_to_close_timeout=..., heartbeat_timeout=..., retry_policy_max_attempts=..., retry_policy_backoff_coefficient=...)` |
| `workflow.execute_activity(fn, arg)` / `args=[a, b]` | `await fn(arg)` / `await fn(a, b)` |
| `workflow.start_activity(fn, arg)` | `asyncio.create_task(fn(arg))` |
| `workflow.execute_local_activity(...)` | a regular activity call (reported) |
| synchronous `def` activity | `async def` that runs the original body with `asyncio.to_thread` (or a plain `async def` with `--sync-activities async`) |
| `activity.heartbeat(...)` | `activity_heartbeat(...)` |
| `workflow.execute_child_workflow(Child.run, x, id=..., execution_timeout=..., parent_close_policy=...)` | `workflow.execute_workflow(Child, params=..., execution_id=..., execution_timeout=..., parent_close_policy=...)` |
| `workflow.start_child_workflow(...)` | `workflow.execute_workflow(..., wait=False)` |
| `workflow.continue_as_new(x)` | `workflow.continue_as_new(params)` |
| `-> NoReturn` / `-> Never` | `-> None` |
| no return annotation on a handler or activity that returns nothing | `-> None` added |
| any other `workflow.X` / `activity.X` (`logger`, `info()`, `patched()`, `all_handlers_finished()`...) | `temporal_workflow.X` / `temporal_activity.X`, imported with `from temporalio import workflow as temporal_workflow`. This works because Mistral workers run on Temporal |

### Activity options

Timeouts and retries are collected from every call site in the project, including module-level
constants like `RETRY = RetryPolicy(maximum_attempts=3)`. If two call sites disagree, the tool keeps
the most permissive value and tells you. If `schedule_to_close_timeout` is the only timeout, it becomes
`start_to_close_timeout`. Temporal's unlimited retries become `retry_policy_max_attempts=0`, unless you
pass `--mistral-retry-defaults`.

### Workflow inputs

Child workflows and `continue_as_new` take one pydantic model on Mistral. If the target's entrypoint
already takes a single pydantic model, the argument is passed through. Otherwise the tool writes a small
model next to the caller:

```python
class ComposeGreetingWorkflowParams(BaseModel):
    """Input of the `ComposeGreetingWorkflow` workflow (generated by temporal2mistral)."""

    input: Any

...
return await workflow.execute_workflow(
    ComposeGreetingWorkflow, params=ComposeGreetingWorkflowParams(input=ComposeGreetingInput("Hello", name))
)
```

The child still validates its input against its own annotations, so dataclasses keep working.

## What you still have to do

The tool reports these and leaves the code alone:

- Worker and client code. `Worker(...)` becomes `await run_worker([...workflows])`, and activities are
  registered automatically. Starting, signalling and querying workflows goes through the Mistral API
  (`Mistral(api_key=...).workflows.execute_workflow(workflow_identifier=..., input={...})`). The report
  points at each place and gives you the `run_worker` call.
- Class-based activities (`execute_activity_method`) and activities defined inside a function.
  Mistral activities are module-level functions. Use `Depends(...)` to share clients.
- `@workflow.init`, update validators, dynamic workflows and handlers, Nexus, asynchronous activity
  completion, and query or update handlers that return nothing.
- Types with no JSON schema, such as plain classes that relied on a custom Temporal payload converter.
- Execution timeouts. Every workflow gets a warning until you set `execution_timeout`, or you can pass
  `--execution-timeout 30d` to set it on all of them.
- Workflows already running on your Temporal cluster. They can't be moved: let them finish there and
  start new executions on Mistral.

## Options

```text
temporal2mistral check PATH... [--fail-on error|warning|never]
temporal2mistral apply PATH... [--diff | --out DIR]

  --execution-timeout DURATION   set execution_timeout on every workflow (30d, 12h, 90m...)
  --sync-activities thread|async how to convert synchronous activities (default: thread)
  --mistral-retry-defaults       use Mistral's 3 attempts where Temporal retried forever
  --import-check                 import every converted module with the Mistral SDK (see below)
  --import-root DIR              directory put on sys.path for --import-check
  --exclude GLOB                 skip paths (repeatable)
  --min-severity error|warning|info
  --format text|json             --report FILE writes the report to a file
```

`check` exits with status 1 when it finds errors, so you can run it in CI:

```console
temporal2mistral check src --format json --report migration-report.json
```

### `--import-check`

Some problems only show up when the code runs: forward references in input types, third-party types
without a schema, and so on. With `--import-check`, each converted module is imported in a subprocess,
from a converted copy of the project, with `mistralai-workflows` installed. Mistral validates
definitions at import time, so anything it rejects is reported as `T2M090` with the SDK's own message.
If the original module doesn't import either (a missing dependency, for example), it is skipped and
reported as `T2M091`.

Be aware that this runs your project's top-level code.

## Diagnostics

Errors mean the converted code won't work until someone fixes it. Warnings mean it works but may behave
differently. Infos record what was changed.

| Rule | Severity | Meaning |
|---|---|---|
| T2M000 | error | File could not be parsed |
| T2M001 | error | Dynamic workflow, activity or handler is not supported |
| T2M002 | error | Missing parameter type annotation (required by Mistral) |
| T2M003 | info | Added `-> None` return annotation (required by Mistral) |
| T2M004 | error | Missing return type annotation (required by Mistral) |
| T2M005 | warning | `NoReturn`/`Never` return annotation replaced by `None` |
| T2M006 | error | Type has no JSON schema (plain class needing a custom converter) |
| T2M010 | warning | Workflow execution timeout now defaults to 1 hour |
| T2M011 | warning | `@workflow.defn` option has no Mistral equivalent and was dropped |
| T2M012 | error | `@workflow.init` is not supported by Mistral |
| T2M013 | error | Update validators are not supported by Mistral |
| T2M014 | warning | Handler option has no Mistral equivalent and was dropped |
| T2M015 | error | Query and update handlers must return a value in Mistral |
| T2M020 | info | Activity call-site options moved to the `@activity` decorator |
| T2M021 | warning | Call sites disagree on activity options; the most permissive value was kept |
| T2M022 | warning | Activity option is not a literal and could not be moved |
| T2M023 | warning | Activity call option has no Mistral equivalent and was dropped |
| T2M024 | warning | RetryPolicy field has no Mistral equivalent and was dropped |
| T2M025 | info | No call site found for activity; Mistral defaults apply |
| T2M026 | info | Unlimited retries kept (Temporal default) instead of Mistral's 3 attempts |
| T2M030 | error | Class-based (method) activities are not supported by Mistral |
| T2M031 | warning | Synchronous activity made asynchronous |
| T2M032 | error | Activity call target could not be resolved to an activity definition |
| T2M033 | warning | `start_activity` converted to `asyncio.create_task` |
| T2M034 | info | Local activity converted to a regular activity |
| T2M035 | warning | Activity chosen at runtime: call converted, options not moved |
| T2M036 | error | Activities defined inside a function are not supported by Mistral |
| T2M040 | info | Child workflow call converted to `workflow.execute_workflow` |
| T2M041 | error | Child workflow target could not be resolved |
| T2M042 | warning | Child workflow option has no Mistral equivalent and was dropped |
| T2M043 | warning | Optional argument omitted with a non-literal default; check the params model |
| T2M050 | error | `continue_as_new` option is not supported by Mistral |
| T2M060 | info | Temporal API kept through a direct `temporalio` import |
| T2M061 | warning | Temporal API kept, but Mistral offers a dedicated equivalent |
| T2M062 | error | Temporal API has no Mistral support (Nexus, async completion, ...) |
| T2M070 | warning | Temporal `Worker` must be replaced by `run_worker` |
| T2M071 | warning | Temporal client calls must be replaced by the Mistral API client |
| T2M090 | error | The Mistral SDK rejects the converted module at import time |
| T2M091 | info | Module not import-checked: the original does not import in this environment |

## Tested on temporalio/samples-python

I test against Temporal's official [Python samples](https://github.com/temporalio/samples-python)
(commit `b0e4d84`, 9 October 2026): 734 Python files outside `tests/`, 236 of which define workflows or
activities.

| | |
|---|---:|
| Modules converted | 236 |
| Converted modules that don't compile | 0 |
| Modules importable here (dependencies installed) | 115 |
| … accepted by `mistralai-workflows` 3.15 at import | 108 |
| … with no error diagnostic, accepted by the SDK | 86 / 86 |
| … rejected by the SDK with no error diagnostic | 0 |

The other 121 modules need optional integrations that weren't installed (OpenAI Agents, LangGraph,
Strands, Google ADK, boto3...). They were converted but not import-checked. All 7 rejected modules
already have error diagnostics: one uses a plain class as input (`T2M006`), one has an update handler
that returns `None` (`T2M015`), and five have recursive or forward-referenced input types and were
already flagged for `@workflow.init` (`T2M012`) or a dynamic activity call (`T2M032`). For those,
`--import-check` adds the SDK's exact message (`T2M090`).

To reproduce: `python scripts/validate_corpus.py path/to/samples-python --out /tmp/converted`.
The full list of errors on the samples is in [docs/samples-python-errors.txt](docs/samples-python-errors.txt).

Two more tests go beyond imports. `tests/test_sdk_validation.py` runs a converted workflow (child
workflow, sync and async activities, retry policy, signal, query and update) in the Mistral SDK's local
mode. `tests/test_e2e_temporal.py` runs one on Temporal's test server through the Mistral SDK's test
worker. That one downloads the test server, so it runs in CI.

## Development

```console
pip install -e ".[test,mistral]" ruff
pytest                                   # the SDK tests run when mistralai-workflows is installed
T2M_SAMPLES=/path/to/samples-python pytest
ruff check . && ruff format src tests scripts
```

[CONTRIBUTING.md](CONTRIBUTING.md) explains how the code is organised and the style rules.

## Roadmap

- Rewrite simple worker entry points to `run_worker([...])`.
- Rewrite `client.execute_workflow(...)` and signal/query calls to the Mistral API client.
- Turn class-based activities that only hold clients into module-level functions using `Depends`.
- Optionally convert input dataclasses to pydantic models.

## License

[MIT](LICENSE)

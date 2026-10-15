# Changelog

## Unreleased

- Input types defined in files that do not import `temporalio` (a separate `models.py`, for example)
  are now found: pydantic models are passed to child workflows as is, and plain classes get `T2M006`.
- `T2M006` looks a type up where the file imports it from, instead of matching every class with the
  same name in the project (this removed a false positive on temporalio/samples-python).
- `workflow` or `activity` used as a value (`api = workflow`) is kept as `temporal_workflow` /
  `temporal_activity` instead of silently pointing at Mistral's module.
- New rule `T2M036`: activities defined inside a function are reported and left unconverted, along with
  their calls.
- An invalid `--execution-timeout` is a usage error instead of a traceback.

## 0.1.0 — 2026-10-15

First release.

- `temporal2mistral check` and `temporal2mistral apply` (in place, `--diff`, `--out`).
- Workflows: `@workflow.defn` → `@workflow.define(name=...)`, `@workflow.run` → `@workflow.entrypoint`,
  signal/query/update handlers, `wait_condition`, `sleep`, `now`, `uuid4`, `random`, `unsafe.*`.
- Activities: `@activity.defn` → `@activity(...)` with the call-site options (timeouts, retries) moved
  to the definition across modules; `execute_activity(fn, ...)` → `await fn(...)`; synchronous
  activities run through `asyncio.to_thread`; `activity.heartbeat` → `activity_heartbeat`.
- Child workflows and `continue_as_new` mapped to Mistral's pydantic-model inputs, with generated
  params models when the target takes dataclasses or primitives.
- 38 diagnostic rules covering what cannot be converted automatically, as text or JSON.
- `--import-check`: import every converted module with the real `mistralai-workflows` SDK.
- Validated on temporalio/samples-python (see README).

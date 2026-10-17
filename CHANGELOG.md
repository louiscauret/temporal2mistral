# Changelog

## 0.1.0 — 2026-10-17

First release. Needs Python 3.12 or later, like `mistralai-workflows`.

- `temporal2mistral check` and `temporal2mistral apply` (in place, `--diff`, `--out`).
- Workflows: `@workflow.defn` → `@workflow.define(name=...)`, `@workflow.run` → `@workflow.entrypoint`,
  signal/query/update handlers, `wait_condition`, `sleep`, `now`, `uuid4`, `random`, `unsafe.*`.
- Activities: `@activity.defn` → `@activity(...)` with the call-site options (timeouts, retries) moved
  to the definition across modules; `execute_activity(fn, ...)` → `await fn(...)`; synchronous
  activities run through `asyncio.to_thread`; `activity.heartbeat` → `activity_heartbeat`.
- Child workflows and `continue_as_new` mapped to Mistral's pydantic-model inputs, with generated
  params models when the target takes dataclasses or primitives. Input types are found in every file
  of the project, including the ones that do not import `temporalio`.
- Activity methods of classes built without arguments become module-level activities that get the
  instance through `Depends`.
- Worker entry points that just run until stopped become `await run_worker([...])`.
- Client code (`execute_workflow`, `start_workflow`, and the handle's `result`, `signal`, `query`,
  `execute_update`) becomes calls to the Mistral API client, function by function, when every use of
  the client can be converted.
- 42 diagnostic rules covering what cannot be converted automatically, as text or JSON.
- `--import-check`: import every converted module with the real `mistralai-workflows` SDK, and report
  each rejection in the module where it happens.
- Validated on temporalio/samples-python (see README).

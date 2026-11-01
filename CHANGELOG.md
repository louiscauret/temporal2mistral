# Changelog

## 0.1.1 — 2026-11-01

Maintenance release. The conversion code has not changed: `check` and `apply` give the same output
as 0.1.0. No regression is known.

### Changed

- The `mistral` extra now requires `mistralai-workflows>=3.17` instead of `>=3.15`. Tests and the
  samples validation run against 3.17, which is the version this release supports.
- The samples validation in the README is refreshed: `temporalio/samples-python` at commit `6e48768`,
  261 modules converted, none failing to compile, 123 of the 130 importable modules accepted by the
  SDK, and no rejection without an error diagnostic. The acceptance results match 0.1.0; the module
  count grew because the samples did.

### Added

- `SECURITY.md`: vulnerabilities are now reported privately through GitHub.

### Upgrade notes

- Without the `mistral` extra, nothing changes: the tool does not depend on the Mistral SDK.
- With the extra, `pip install -U "temporal2mistral[mistral]"` upgrades the SDK to 3.17 or later.
  This matters only for `--import-check`, which imports your converted code with that SDK. Check
  that the rest of your project accepts the new version, as it also brings a recent `mistralai`
  client, which uses `httpx2` instead of `httpx`.
- Pass an `httpx2` client if your code gives the Mistral API client its own HTTP client: `httpx`
  clients are rejected by recent `mistralai` releases. The `T2M071` translations don't do this, so
  they are unchanged.
- Staying on `mistralai-workflows` 3.15 remains possible with 0.1.0 and likely works with this
  release, but it is no longer tested.

## 0.1.0 — 2026-10-18

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
- Client calls (`execute_workflow`, `start_workflow`, and the handle's `signal`, `query`,
  `execute_update`) are reported one by one with the Mistral API client code to write instead.
- 41 diagnostic rules covering what cannot be converted automatically, as text or JSON.
- `--import-check`: import every converted module with the real `mistralai-workflows` SDK, and report
  each rejection in the module where it happens.
- Validated on temporalio/samples-python (see README).

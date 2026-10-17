# Contributing

Thanks for helping! This project aims to stay small and easy to read: a developer who has
never used LibCST should be able to fix a bug after reading this page and one module.

## How it works

The migration runs in two passes over the project (`project.py`):

1. **Scan** (`scan.py`) reads every file and records the workflows, the activities and every
   `workflow.execute_activity(...)` call with its options. This has to happen first because an
   activity and its calls usually live in different files.
2. **Rewrite** (`rewrite.py`) walks each file with a LibCST transformer and hands every node it
   cares about to the module that knows how to rewrite it. Worker scripts need a look at a whole
   function first: `workers.py` decides when entering a function and records the new statements in
   `ctx.replacements`, which the transformer applies on its way out.

| Module | Job |
|---|---|
| `cli.py` | Command line: parse arguments, write files, print the report |
| `project.py` | Find the Python files, run the scan, then rewrite each file |
| `scan.py` | First pass: workflows, activities, activity calls |
| `rewrite.py` | Second pass: the LibCST transformer, and import clean-up |
| `workflows.py` | Workflow classes, handlers, child workflows, continue-as-new |
| `activities.py` | Activity definitions and calls |
| `activity_options.py` | Call-site options → `@activity(...)` arguments (pure functions) |
| `signatures.py` | Type annotations Mistral needs |
| `temporal_api.py` | Every other `workflow.X` / `activity.X` |
| `workers.py` | `Worker(...)` entry points → `run_worker([...])` |
| `clients.py` | Temporal client calls, reported with the Mistral code to write instead |
| `calls.py` | Read the arguments of Temporal calls |
| `syntax.py` | Small LibCST helpers and the import table |
| `durations.py` | `timedelta(...)` values |
| `context.py` | State shared while rewriting one file, and the `Options` |
| `diagnostics.py` | The rules (T2Mxxx) and the list of diagnostics |
| `report.py` | Text and JSON output |
| `import_check.py` | `--import-check`: import the converted code with the Mistral SDK |

## Code style

- **Plain code over clever code.** Explicit loops and `if`/`else` are fine; nested comprehensions,
  one-line conditionals and metaprogramming are not.
- **Generate new code from strings.** Build the replacement with
  `cst.parse_expression(f"workflow.define(name={name})")` rather than by assembling nodes by hand,
  and reuse the original pieces with `code_of(node)`.
- **Names resolve through the import table.** `full_name(node, imports)` turns `workflow.defn` into
  `temporalio.workflow.defn`. Local variables that shadow an import are not tracked, on purpose.
- **Report instead of guessing.** When a conversion is not certain to keep the behaviour, leave
  the code as it is and add a diagnostic with a hint saying what to do.
- **Small modules, one job each**, with a docstring at the top showing the before/after it handles.
- **Comments explain why**, not what the next line does.
- Format with `ruff format` and keep `ruff check` clean (120 columns).

## Adding a conversion

1. Write a test in `tests/test_transform.py` with the Temporal input and the expected output.
2. Add the rewrite to the module that owns that part of the API (see the table above).
3. If something can go wrong, add a rule to `diagnostics.py` **and** to the table in `README.md`
   (a test checks they match).
4. Run `scripts/validate_corpus.py` on temporalio/samples-python: it must still report
   `rejected without error diagnostic: 0`.

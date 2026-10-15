"""``--import-check``: let the real Mistral SDK judge the converted code.

Importing a converted module runs Mistral's definition-time validation
(`@workflow.define`, `@activity`, handler signatures, JSON schemas), which catches
what static analysis cannot, such as forward references in input types.

Each module is imported in a separate process from a converted copy of the
project. When it fails, the original module is imported too: if the original
fails as well, a dependency is simply missing and nothing is reported against
the migration. Importing runs the project's top-level code.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .diagnostics import Diagnostics
from .project import SKIPPED_DIRECTORIES, MigrationResult

# Runs in the subprocess: import a module and print the error, if any, as JSON.
IMPORT_SCRIPT = """
import importlib, json, sys, traceback
root, module, path = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, root)
try:
    importlib.import_module(module)
except BaseException as error:
    lines = [frame.lineno for frame in traceback.extract_tb(error.__traceback__) if frame.filename == path]
    message = str(error).strip().splitlines()[0] if str(error).strip() else ""
    print(json.dumps({"type": type(error).__name__, "message": message, "line": lines[-1] if lines else 0}))
    sys.exit(1)
"""

QUIET_SDK = {"LOG_LEVEL": "ERROR", "OTEL_ENABLED": "false", "PYTHONDONTWRITEBYTECODE": "1"}


def mistral_sdk_installed() -> bool:
    try:
        return importlib.util.find_spec("mistralai.workflows") is not None
    except ModuleNotFoundError:
        return False


def _module_name(root: Path, path: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _import_error(root: Path, module: str, path: Path, timeout: int) -> dict | None:
    """Import ``module`` with ``root`` on sys.path; return the error, or None if it imports."""
    try:
        process = subprocess.run(
            [sys.executable, "-c", IMPORT_SCRIPT, str(root), module, str(path)],
            cwd=root,
            env={**os.environ, **QUIET_SDK},
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"type": "Timeout", "message": f"import took more than {timeout}s", "line": 0}
    if process.returncode == 0:
        return None
    last_line = process.stdout.strip().splitlines()[-1] if process.stdout.strip() else ""
    try:
        return json.loads(last_line)
    except ValueError:
        stderr = process.stderr.strip().splitlines()
        return {"type": "Error", "message": stderr[-1] if stderr else "unknown error", "line": 0}


def check_imports(result: MigrationResult, import_root: Path | None = None, timeout: int = 60) -> Diagnostics:
    """Import every converted module; return T2M090 (rejected) and T2M091 (not checkable) diagnostics."""
    diagnostics = Diagnostics()
    root = (import_root or result.root).resolve()
    converted = [f for f in result.files if f.uses_temporal and f.path.resolve().is_relative_to(root)]
    if not converted:
        return diagnostics

    with tempfile.TemporaryDirectory(prefix="temporal2mistral-") as temporary:
        copy = Path(temporary) / "project"
        shutil.copytree(root, copy, ignore=shutil.ignore_patterns(*SKIPPED_DIRECTORIES))
        for file in converted:
            (copy / file.path.resolve().relative_to(root)).write_text(file.converted, encoding="utf-8")

        for file in sorted(converted, key=lambda f: str(f.path)):
            original_path = file.path.resolve()
            copied_path = copy / original_path.relative_to(root)
            module = _module_name(root, original_path)

            error = _import_error(copy, module, copied_path, timeout)
            if error is None:
                continue
            original_error = _import_error(root, module, original_path, timeout)
            if original_error is not None:
                diagnostics.add(
                    "T2M091",
                    str(file.path),
                    0,
                    f"Not checked: the original module does not import here "
                    f"({original_error['type']}: {original_error['message']})",
                    "Install the project's dependencies (and set --import-root) to check it.",
                )
            else:
                diagnostics.add(
                    "T2M090",
                    str(file.path),
                    error["line"],
                    f"{error['type']}: {error['message']}",
                    "Line numbers refer to the converted file.",
                )
    return diagnostics

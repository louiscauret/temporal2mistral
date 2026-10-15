"""Print a migration result as text (for people) or JSON (for CI and other tools)."""

from __future__ import annotations

import json
import os

from . import __version__
from .diagnostics import SEVERITY_ORDER, Diagnostic, Severity
from .project import MigrationResult

COLORS = {Severity.ERROR: "\033[31m", Severity.WARNING: "\033[33m", Severity.INFO: "\033[36m"}
RESET_COLOR = "\033[0m"


def _relative(result: MigrationResult, path: str) -> str:
    return os.path.relpath(path, result.root)


def _shown(result: MigrationResult, min_severity: Severity) -> list[Diagnostic]:
    """Diagnostics at least as severe as ``min_severity``."""
    allowed = SEVERITY_ORDER[: SEVERITY_ORDER.index(min_severity) + 1]
    return [d for d in result.diagnostics.sorted() if d.severity in allowed]


def summary(result: MigrationResult) -> dict[str, int]:
    return {
        "files_scanned": result.files_scanned,
        "files_using_temporalio": len(result.files),
        "files_converted": len([f for f in result.files if f.uses_temporal and f.changed]),
        "errors": result.diagnostics.count(Severity.ERROR),
        "warnings": result.diagnostics.count(Severity.WARNING),
        "infos": result.diagnostics.count(Severity.INFO),
    }


def summary_line(result: MigrationResult) -> str:
    counts = summary(result)
    return (
        f"{counts['files_converted']} file(s) converted out of {counts['files_using_temporalio']} using temporalio "
        f"({counts['files_scanned']} scanned): {counts['errors']} error(s), {counts['warnings']} warning(s), "
        f"{counts['infos']} info(s)."
    )


def text_report(result: MigrationResult, min_severity: Severity = Severity.INFO, color: bool = False) -> str:
    lines = []
    current_path = None
    for diagnostic in _shown(result, min_severity):
        path = _relative(result, diagnostic.path)
        if path != current_path:
            if current_path is not None:
                lines.append("")
            lines.append(path)
            current_path = path
        severity = diagnostic.severity.value.upper()
        if color:
            severity = COLORS[diagnostic.severity] + severity + RESET_COLOR
        lines.append(f"  {diagnostic.line:>4}  {severity:<7} {diagnostic.code}  {diagnostic.message}")
        if diagnostic.hint:
            lines.append(f"               -> {diagnostic.hint}")
    if lines:
        lines.append("")
    lines.append(summary_line(result))
    return "\n".join(lines)


def json_report(result: MigrationResult, min_severity: Severity = Severity.INFO) -> str:
    diagnostics = []
    for diagnostic in _shown(result, min_severity):
        diagnostics.append(
            {
                "code": diagnostic.code,
                "severity": diagnostic.severity.value,
                "path": _relative(result, diagnostic.path),
                "line": diagnostic.line,
                "title": diagnostic.title,
                "message": diagnostic.message,
                "hint": diagnostic.hint,
            }
        )
    files = []
    for file in sorted(result.files, key=lambda f: str(f.path)):
        files.append({"path": _relative(result, str(file.path)), "converted": file.uses_temporal and file.changed})
    report = {
        "tool": "temporal2mistral",
        "version": __version__,
        "summary": summary(result),
        "files": files,
        "diagnostics": diagnostics,
    }
    return json.dumps(report, indent=2, ensure_ascii=False)

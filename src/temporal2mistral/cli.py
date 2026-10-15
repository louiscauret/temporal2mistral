"""Command line interface: `temporal2mistral check` and `temporal2mistral apply`."""

from __future__ import annotations

import argparse
import difflib
import os
import shutil
import sys
from pathlib import Path

from . import __version__
from .context import Options
from .diagnostics import Severity
from .durations import parse_cli_duration
from .import_check import check_imports, mistral_sdk_installed
from .project import SKIPPED_DIRECTORIES, MigrationResult, migrate
from .report import json_report, summary_line, text_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="temporal2mistral",
        description="Migrate Temporal Python workflows to Mistral Workflows (mistralai-workflows).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="report what the migration needs, without writing anything")
    _add_common_arguments(check)
    check.add_argument(
        "--fail-on",
        choices=["error", "warning", "never"],
        default="error",
        help="exit with status 1 when diagnostics of this severity exist (default: error)",
    )

    apply = commands.add_parser("apply", help="rewrite the code, in place or into --out")
    _add_common_arguments(apply)
    output = apply.add_mutually_exclusive_group()
    output.add_argument("--diff", action="store_true", help="print a unified diff instead of writing files")
    output.add_argument("--out", type=Path, metavar="DIR", help="copy the project to DIR and convert the copy")
    return parser


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("paths", nargs="+", type=Path, help="files or directories to migrate")
    parser.add_argument(
        "--execution-timeout",
        type=_duration,
        metavar="DURATION",
        help="set execution_timeout on every workflow (e.g. 30d, 12h); Mistral defaults to 1h",
    )
    parser.add_argument(
        "--sync-activities",
        choices=["thread", "async"],
        default="thread",
        help="convert synchronous activities by running their body with asyncio.to_thread (default) "
        "or by making them `async def`",
    )
    parser.add_argument(
        "--mistral-retry-defaults",
        action="store_true",
        help="use Mistral's default of 3 attempts where Temporal retried forever",
    )
    parser.add_argument(
        "--exclude", action="append", default=[], metavar="GLOB", help="skip matching paths (repeatable)"
    )
    parser.add_argument(
        "--min-severity", choices=["error", "warning", "info"], default="info", help="hide less severe diagnostics"
    )
    parser.add_argument("--format", choices=["text", "json"], default="text", help="report format")
    parser.add_argument("--report", type=Path, metavar="FILE", help="write the report to FILE instead of stdout")
    parser.add_argument(
        "--import-check",
        action="store_true",
        help="import every converted module with the Mistral SDK to catch errors it reports at definition time "
        "(needs mistralai-workflows; runs the project's top-level code)",
    )
    parser.add_argument("--import-root", type=Path, metavar="DIR", help="directory put on sys.path for --import-check")


def _duration(text: str) -> float:
    """Seconds in a duration such as `30d` or `12h`; argparse reports the error otherwise."""
    try:
        seconds = parse_cli_duration(text)
    except ValueError:
        seconds = 0
    if seconds <= 0:
        raise argparse.ArgumentTypeError(f"invalid duration {text!r} (use e.g. 30d, 12h, 90m, 45s)")
    return seconds


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for path in args.paths:
        if not path.exists():
            print(f"temporal2mistral: {path} does not exist", file=sys.stderr)
            return 2

    options = Options(
        execution_timeout=args.execution_timeout,
        sync_activities=args.sync_activities,
        keep_unlimited_retries=not args.mistral_retry_defaults,
    )
    result = migrate(args.paths, options, args.exclude)

    if args.import_check:
        if mistral_sdk_installed():
            result.diagnostics.extend(check_imports(result, args.import_root))
        else:
            print("temporal2mistral: --import-check skipped, mistralai-workflows is not installed", file=sys.stderr)

    if args.command == "check":
        _print_report(args, result)
        return _check_exit_status(args.fail_on, result)

    if args.diff:
        _print_diff(result)
        if args.report:
            _print_report(args, result)
        return 0

    if args.out:
        error = _write_copy(args.paths, args.out, result)
        if error:
            print(f"temporal2mistral: {error}", file=sys.stderr)
            return 2
    else:
        for file in result.changed_files:
            file.path.write_text(file.converted, encoding="utf-8")
    _print_report(args, result)
    return 0


def _check_exit_status(fail_on: str, result: MigrationResult) -> int:
    if fail_on == "never":
        return 0
    if fail_on == "warning":
        failing = (Severity.ERROR, Severity.WARNING)
    else:
        failing = (Severity.ERROR,)
    return 1 if any(d.severity in failing for d in result.diagnostics.items) else 0


def _print_report(args: argparse.Namespace, result: MigrationResult) -> None:
    min_severity = Severity(args.min_severity)
    if args.format == "json":
        report = json_report(result, min_severity)
    else:
        use_color = args.report is None and sys.stdout.isatty() and "NO_COLOR" not in os.environ
        report = text_report(result, min_severity, color=use_color)

    if args.report is None:
        print(report)
        return
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report + "\n", encoding="utf-8")
    print(summary_line(result), file=sys.stderr)
    print(f"Report written to {args.report}", file=sys.stderr)


def _print_diff(result: MigrationResult) -> None:
    for file in result.changed_files:
        relative = os.path.relpath(file.path, result.root)
        diff = difflib.unified_diff(
            file.original.splitlines(keepends=True),
            file.converted.splitlines(keepends=True),
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
        )
        sys.stdout.writelines(diff)


def _write_copy(paths: list[Path], out: Path, result: MigrationResult) -> str | None:
    """Copy the project to ``out`` and write the converted files there. Return an error message, if any."""
    if len(paths) != 1 or not paths[0].is_dir():
        return "--out needs a single directory argument"
    if out.exists() and any(out.iterdir()):
        return f"{out} is not empty"
    shutil.copytree(paths[0], out, ignore=shutil.ignore_patterns(*SKIPPED_DIRECTORIES), dirs_exist_ok=True)
    for file in result.changed_files:
        (out / file.path.relative_to(paths[0])).write_text(file.converted, encoding="utf-8")
    return None


if __name__ == "__main__":
    sys.exit(main())

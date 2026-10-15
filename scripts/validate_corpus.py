"""Measure how well temporal2mistral converts a whole code base, such as temporalio/samples-python.

    python scripts/validate_corpus.py path/to/samples-python --out /tmp/converted

1. converts the project into --out and checks that every converted file compiles;
2. runs `temporal2mistral check --import-check`, which imports every converted module with
   the real Mistral SDK (mistralai-workflows must be installed);
3. checks that every module the SDK rejects also has an error diagnostic, i.e. that the
   tool never lets a broken module through silently.

Importing runs the project's top-level code: only run this on code you trust.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from temporal2mistral.cli import main as temporal2mistral

IMPORT_CHECK_CODES = ("T2M090", "T2M091")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--out", type=Path, required=True, help="empty directory for the converted copy")
    parser.add_argument("--exclude", action="append", default=["tests"])
    args = parser.parse_args()

    excludes = [option for pattern in args.exclude for option in ("--exclude", pattern)]
    report_path = args.out / "temporal2mistral-report.json"
    apply_report = args.out / "temporal2mistral-apply.txt"
    if (
        temporal2mistral(["apply", str(args.source), "--out", str(args.out), *excludes, "--report", str(apply_report)])
        != 0
    ):
        return 2
    temporal2mistral(
        [
            "check",
            str(args.source),
            *excludes,
            "--import-check",
            "--format",
            "json",
            "--report",
            str(report_path),
            "--fail-on",
            "never",
        ]
    )
    report = json.loads(report_path.read_text())

    converted = [file["path"] for file in report["files"] if file["converted"]]
    not_compiling = []
    for path in converted:
        try:
            compile((args.out / path).read_text(), path, "exec")
        except SyntaxError:
            not_compiling.append(path)

    rejected = {d["path"] for d in report["diagnostics"] if d["code"] == "T2M090"}
    not_checked = {d["path"] for d in report["diagnostics"] if d["code"] == "T2M091"}
    with_static_error = {
        d["path"] for d in report["diagnostics"] if d["severity"] == "error" and d["code"] not in IMPORT_CHECK_CODES
    }
    checked = [path for path in converted if path not in not_checked]
    clean = [path for path in checked if path not in with_static_error]
    silently_broken = sorted(rejected - with_static_error)

    print(f"converted modules:                  {len(converted)}")
    print(f"  not compiling:                    {len(not_compiling)}")
    print(f"  import-checked with the SDK:      {len(checked)} ({len(not_checked)} skipped: dependencies missing)")
    print(f"  accepted by the SDK:              {len(checked) - len(rejected)}")
    print(f"  without error diagnostic:         {len(clean)}, accepted: {len([p for p in clean if p not in rejected])}")
    print(f"  rejected without error diagnostic: {len(silently_broken)} {silently_broken}")
    return 1 if not_compiling or silently_broken else 0


if __name__ == "__main__":
    sys.exit(main())

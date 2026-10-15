from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from temporal2mistral.context import Options
from temporal2mistral.diagnostics import Diagnostic
from temporal2mistral.project import migrate


@dataclass
class Converted:
    files: dict[str, str]  # relative path -> converted source
    diagnostics: list[Diagnostic]

    def codes(self) -> list[str]:
        return [d.code for d in self.diagnostics]

    def messages(self, code: str) -> list[str]:
        return [d.message for d in self.diagnostics if d.code == code]


def write_files(root: Path, files: dict[str, str]) -> None:
    for relative_path, source in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")


@pytest.fixture
def convert(tmp_path: Path):
    """Write the given files to a temporary project, migrate it, return the converted sources."""

    def run(files: dict[str, str], **options) -> Converted:
        root = tmp_path / "project"
        write_files(root, files)
        result = migrate([root], Options(**options))
        converted = {str(f.path.relative_to(root)): f.converted for f in result.files}
        for relative_path in files:
            if relative_path not in converted:
                converted[relative_path] = (root / relative_path).read_text(encoding="utf-8")
        return Converted(converted, result.diagnostics.sorted())

    return run

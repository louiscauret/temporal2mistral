"""Migrate a whole project: find the Python files, scan them all, then rewrite each one."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import libcst as cst

from .context import Options
from .diagnostics import Diagnostics
from .rewrite import rewrite_file
from .scan import scan_project

SKIPPED_DIRECTORIES = {
    ".git",
    ".hg",
    ".venv",
    "venv",
    "env",
    ".tox",
    ".nox",
    "node_modules",
    "__pycache__",
    "site-packages",
    "build",
    "dist",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}


@dataclass
class ConvertedFile:
    path: Path
    original: str
    converted: str

    @property
    def changed(self) -> bool:
        return self.original != self.converted


@dataclass
class MigrationResult:
    root: Path
    files: list[ConvertedFile] = field(default_factory=list)  # files that mention temporalio
    diagnostics: Diagnostics = field(default_factory=Diagnostics)
    files_scanned: int = 0

    @property
    def changed_files(self) -> list[ConvertedFile]:
        return [f for f in self.files if f.changed]


def find_python_files(paths: list[Path], exclude: list[str]) -> list[Path]:
    """Python files under ``paths``, skipping virtualenvs, caches and the ``exclude`` patterns."""
    found = []
    for path in paths:
        if path.is_file():
            if path.suffix == ".py":
                found.append(path)
            continue
        for directory, subdirectories, filenames in os.walk(path):
            subdirectories[:] = sorted(
                d for d in subdirectories if d not in SKIPPED_DIRECTORIES and not d.endswith(".egg-info")
            )
            for filename in sorted(filenames):
                if not filename.endswith(".py"):
                    continue
                file = Path(directory) / filename
                relative = file.relative_to(path)
                if not _is_excluded(relative, exclude):
                    found.append(file)
    return found


def _is_excluded(relative: Path, patterns: list[str]) -> bool:
    """``tests`` excludes the `tests/` directory; ``*_test.py`` excludes matching files."""
    for pattern in patterns:
        if relative.match(pattern) or relative.parts[0] == pattern.rstrip("/"):
            return True
    return False


def project_root(paths: list[Path]) -> Path:
    if len(paths) == 1:
        return paths[0] if paths[0].is_dir() else paths[0].parent
    return Path(os.path.commonpath([str(path.resolve()) for path in paths]))


def migrate(paths: list[Path], options: Options | None = None, exclude: list[str] | None = None) -> MigrationResult:
    options = options or Options()
    result = MigrationResult(root=project_root(paths))

    sources: dict[Path, str] = {}
    modules: dict[Path, cst.Module] = {}
    for path in find_python_files(paths, exclude or []):
        result.files_scanned += 1
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            result.diagnostics.add("T2M000", str(path), 0, f"Could not read file: {error}")
            continue
        uses_temporal = "temporalio" in source
        # Other files are only scanned for their classes: input types often live in a separate models.py.
        if not uses_temporal and "class " not in source:
            continue
        try:
            modules[path] = cst.parse_module(source)
        except cst.ParserSyntaxError as error:
            if uses_temporal:
                result.diagnostics.add("T2M000", str(path), error.raw_line, f"Syntax error: {error.message}")
            continue
        if uses_temporal:
            sources[path] = source

    project = scan_project(result.root, modules)
    for path, source in sources.items():
        converted = rewrite_file(source, path, project, options, result.diagnostics)
        result.files.append(ConvertedFile(path, source, converted))
    return result

"""State shared by the functions that rewrite one file."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import libcst as cst
from libcst.codemod import CodemodContext
from libcst.metadata import CodeRange

from .diagnostics import Diagnostics
from .scan import Project, Workflow
from .syntax import full_name

# Names used in the converted code to keep Temporal APIs that Mistral does not wrap
TEMPORAL_WORKFLOW_ALIAS = "temporal_workflow"
TEMPORAL_ACTIVITY_ALIAS = "temporal_activity"


@dataclass
class Options:
    execution_timeout: float | None = None  # seconds; set on every workflow when given
    sync_activities: str = "thread"  # "thread": run the body with asyncio.to_thread; "async": make it `async def`
    keep_unlimited_retries: bool = True  # Temporal retries forever by default, Mistral 3 times


@dataclass
class FileContext:
    path: Path
    project: Project
    options: Options
    diagnostics: Diagnostics
    imports: dict[str, str]  # import table of the file, see syntax.import_table
    positions: Mapping[cst.CSTNode, CodeRange]  # where each node of the original file is
    # Imports the rewritten code may need, as (module, name, alias). They are only
    # added if the name is actually used in the final code (see rewrite.py).
    needed_imports: set[tuple[str, str | None, str | None]] = field(default_factory=set)
    # Workflows that need a generated pydantic "params" model in this file, by class name
    params_models: dict[str, Workflow] = field(default_factory=dict)
    changed: bool = False
    # Used by LibCST's AddImportsVisitor / RemoveImportsVisitor to queue import changes
    codemod_context: CodemodContext = field(default_factory=CodemodContext)

    def full_name(self, node: cst.BaseExpression) -> str | None:
        return full_name(node, self.imports)

    def need(self, module: str, name: str | None = None, alias: str | None = None) -> None:
        self.needed_imports.add((module, name, alias))

    def report(
        self,
        code: str,
        node: cst.CSTNode,
        message: str,
        hint: str | None = None,
        once_per_file: bool = False,
    ) -> None:
        line = self.positions[node].start.line
        self.diagnostics.add(code, str(self.path), line, message, hint, once_per_file)

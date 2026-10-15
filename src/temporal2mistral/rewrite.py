"""Second pass: rewrite one file from the Temporal SDK to the Mistral Workflows SDK.

`FileRewriter` walks the syntax tree and hands each node it cares about to the
module that knows how to rewrite it:

- workflow classes and their methods, child workflows, continue-as-new: workflows.py
- activity definitions and calls: activities.py
- any other `workflow.X` / `activity.X`: temporal_api.py

Then `rewrite_file` fixes the imports of the result.
"""

from __future__ import annotations

from pathlib import Path

import libcst as cst
from libcst.codemod.visitors import AddImportsVisitor, RemoveImportsVisitor
from libcst.metadata import MetadataWrapper, PositionProvider

from .activities import rewrite_activity_call, rewrite_activity_definition
from .context import TEMPORAL_ACTIVITY_ALIAS, TEMPORAL_WORKFLOW_ALIAS, FileContext, Options
from .diagnostics import Diagnostics
from .scan import ACTIVITY_CALL_APIS, Project, Workflow
from .syntax import code_of, dotted_name, import_table, split_decorator
from .temporal_api import rewrite_temporal_attribute
from .workflows import (
    params_model_code,
    rewrite_child_workflow_call,
    rewrite_continue_as_new,
    rewrite_workflow_decorator,
    rewrite_workflow_method,
)

CLASS_BASED_ACTIVITY_APIS = {
    "temporalio.workflow.execute_activity_method",
    "temporalio.workflow.start_activity_method",
    "temporalio.workflow.execute_local_activity_method",
    "temporalio.workflow.start_local_activity_method",
    "temporalio.workflow.execute_activity_class",
    "temporalio.workflow.start_activity_class",
}
CHILD_WORKFLOW_APIS = {"temporalio.workflow.execute_child_workflow", "temporalio.workflow.start_child_workflow"}

# Imports of these modules are removed once the rewritten code no longer uses them.
TEMPORAL_MODULES_TO_CLEAN = {"temporalio.workflow", "temporalio.activity", "temporalio.common"}


class FileRewriter(cst.CSTTransformer):
    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(
        self, path: Path, project: Project, options: Options, diagnostics: Diagnostics, imports: dict[str, str]
    ) -> None:
        super().__init__()
        self.ctx = FileContext(path, project, options, diagnostics, imports, line_of=self.line_of)
        # Classes we are in, innermost last, with the workflow each one defines (or None)
        self.class_stack: list[tuple[cst.ClassDef, Workflow | None]] = []
        self.reported_temporal_client = False

    def line_of(self, node: cst.CSTNode) -> int:
        return self.get_metadata(PositionProvider, node).start.line

    def _is_temporal(self, node: cst.BaseExpression, name: str) -> bool:
        return self.ctx.full_name(node) == name

    # Imports

    def visit_Import(self, node: cst.Import) -> bool:
        for alias in node.names:
            name = dotted_name(alias.name)
            if name in TEMPORAL_MODULES_TO_CLEAN:
                RemoveImportsVisitor.remove_unused_import(self.ctx.codemod_context, name)
        return False  # names inside import statements are never rewritten

    def visit_ImportFrom(self, node: cst.ImportFrom) -> bool:
        module = dotted_name(node.module) if node.module is not None else None
        if module in TEMPORAL_MODULES_TO_CLEAN and not isinstance(node.names, cst.ImportStar):
            for alias in node.names:
                alias_name = alias.asname.name.value if alias.asname is not None else None
                RemoveImportsVisitor.remove_unused_import(
                    self.ctx.codemod_context, module, dotted_name(alias.name), alias_name
                )
        return False

    def leave_ImportFrom(
        self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom
    ) -> cst.ImportFrom | cst.RemovalSentinel:
        """Drop `workflow` and `activity` from `from temporalio import ...`: every use of
        them is rewritten, and Mistral's own `workflow` and `activity` take their names."""
        module = dotted_name(original_node.module) if original_node.module is not None else None
        if module != "temporalio" or isinstance(updated_node.names, cst.ImportStar):
            return updated_node

        kept = []
        for alias in updated_node.names:
            alias_name = alias.asname.name.value if alias.asname is not None else None
            is_temporal_module = dotted_name(alias.name) in ("workflow", "activity")
            already_converted = alias_name in (TEMPORAL_WORKFLOW_ALIAS, TEMPORAL_ACTIVITY_ALIAS)
            if not is_temporal_module or already_converted:
                kept.append(alias)
        if len(kept) == len(updated_node.names):
            return updated_node
        self.ctx.changed = True
        if not kept:
            return cst.RemoveFromParent()
        kept[-1] = kept[-1].with_changes(comma=cst.MaybeSentinel.DEFAULT)
        return updated_node.with_changes(names=kept)

    # `workflow.X` and `activity.X`

    def leave_Attribute(self, original_node: cst.Attribute, updated_node: cst.Attribute) -> cst.BaseExpression:
        return rewrite_temporal_attribute(self.ctx, original_node, updated_node)

    # Classes

    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        workflow = None
        for decorator in node.decorators:
            target, _ = split_decorator(decorator)
            if self._is_temporal(target, "temporalio.workflow.defn"):
                workflow = self.ctx.project.workflow_defined_at(str(self.ctx.path), node.name.value)
        self.class_stack.append((node, workflow))

    def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.ClassDef:
        _, workflow = self.class_stack.pop()
        if workflow is None:
            return updated_node
        decorators = []
        for original_decorator, decorator in zip(original_node.decorators, updated_node.decorators):
            target, _ = split_decorator(original_decorator)
            if self._is_temporal(target, "temporalio.workflow.defn"):
                decorator = rewrite_workflow_decorator(self.ctx, original_node, original_decorator, decorator)
            decorators.append(decorator)
        return updated_node.with_changes(decorators=decorators)

    def _current_workflow(self) -> Workflow | None:
        for _, workflow in reversed(self.class_stack):
            if workflow is not None:
                return workflow
        return None

    # Functions and methods

    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.FunctionDef | cst.FlattenSentinel:
        is_method = self._is_method(original_node)

        for index, decorator in enumerate(original_node.decorators):
            target, _ = split_decorator(decorator)
            if self._is_temporal(target, "temporalio.activity.defn"):
                return rewrite_activity_definition(self.ctx, original_node, updated_node, index, is_method)

        if not is_method:
            return updated_node
        cls, workflow = self.class_stack[-1]
        if workflow is None:
            return updated_node
        return rewrite_workflow_method(
            self.ctx, original_node, updated_node, cls.name.value, self._update_handlers(cls)
        )

    def _is_method(self, function: cst.FunctionDef) -> bool:
        """True when the function is defined directly in the body of the class we are in."""
        if not self.class_stack:
            return False
        cls, _ = self.class_stack[-1]
        return any(statement is function for statement in cls.body.body)

    def _update_handlers(self, cls: cst.ClassDef) -> set[str]:
        names = set()
        for statement in cls.body.body:
            if isinstance(statement, cst.FunctionDef):
                for decorator in statement.decorators:
                    target, _ = split_decorator(decorator)
                    if self._is_temporal(target, "temporalio.workflow.update"):
                        names.add(statement.name.value)
        return names

    # Calls

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.BaseExpression:
        api = self.ctx.full_name(original_node.func)
        if api is None:
            return updated_node
        short_name = api.split(".")[-1]

        if api in ACTIVITY_CALL_APIS:
            return rewrite_activity_call(self.ctx, original_node, updated_node, short_name)
        if api in CLASS_BASED_ACTIVITY_APIS:
            self.ctx.report(
                "T2M030",
                original_node,
                f"`workflow.{short_name}` calls a class-based activity",
                "Move the activity to a module-level `async def` decorated with `@activity(...)` and call it directly.",
            )
            return updated_node
        if api in CHILD_WORKFLOW_APIS:
            return rewrite_child_workflow_call(self.ctx, original_node, updated_node, short_name)
        if api == "temporalio.workflow.continue_as_new":
            return rewrite_continue_as_new(self.ctx, original_node, updated_node, self._current_workflow())
        if api == "temporalio.worker.Worker":
            self._report_worker(original_node)
        elif api.startswith("temporalio.client.Client") and not self.reported_temporal_client:
            self.reported_temporal_client = True
            self.ctx.report(
                "T2M071",
                original_node,
                "Temporal client in use: starting, signalling and querying workflows goes through the Mistral API",
                "Use `Mistral(api_key=...).workflows.execute_workflow(workflow_identifier=..., input={...})` "
                "and `.workflows.executions.signal_workflow_execution(...)` / `.query_workflow_execution(...)`.",
            )
        return updated_node

    def _report_worker(self, call: cst.Call) -> None:
        workflows = "[...]"
        for arg in call.args:
            if arg.keyword is not None and arg.keyword.value == "workflows":
                workflows = code_of(arg.value)
        self.ctx.report(
            "T2M070",
            call,
            "Temporal `Worker(...)` must be replaced: Mistral registers activities automatically",
            f"`await run_worker({workflows})` (from mistralai.workflows import run_worker).",
        )

    # Module

    def leave_Module(self, original_node: cst.Module, updated_node: cst.Module) -> cst.Module:
        """Insert the generated params models before the first class or function of the file."""
        if not self.ctx.params_models:
            return updated_node
        self.ctx.need("pydantic", "BaseModel")
        self.ctx.need("typing", "Any")
        models = []
        for class_name in sorted(self.ctx.params_models):
            model = cst.parse_statement(params_model_code(self.ctx.params_models[class_name]))
            models.append(model.with_changes(leading_lines=[cst.EmptyLine(), cst.EmptyLine()]))

        body = list(updated_node.body)
        position = len(body)
        for index, statement in enumerate(body):
            if isinstance(statement, (cst.ClassDef, cst.FunctionDef)):
                position = index
                break
        return updated_node.with_changes(body=body[:position] + models + body[position:])


class _UsedNames(cst.CSTVisitor):
    """Names the code refers to, outside import statements (`a.b` refers to `a` only)."""

    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Import(self, node: cst.Import) -> bool:
        return False

    def visit_ImportFrom(self, node: cst.ImportFrom) -> bool:
        return False

    def visit_Attribute(self, node: cst.Attribute) -> bool:
        node.value.visit(self)
        return False

    def visit_Name(self, node: cst.Name) -> None:
        self.names.add(node.value)


def _fix_imports(module: cst.Module, ctx: FileContext) -> cst.Module:
    """Add the imports the rewritten code uses, and remove the Temporal imports it no longer uses."""
    used = _UsedNames()
    module.visit(used)
    for import_module, name, alias in sorted(ctx.needed_imports, key=str):
        bound_name = alias or name or import_module.split(".")[0]
        if bound_name in used.names:
            AddImportsVisitor.add_needed_import(ctx.codemod_context, import_module, name, alias)
    module = RemoveImportsVisitor(ctx.codemod_context).transform_module(module)
    return AddImportsVisitor(ctx.codemod_context).transform_module(module)


def rewrite_file(
    source: str, path: Path, project: Project, options: Options, diagnostics: Diagnostics
) -> tuple[str, bool]:
    """Return the converted source of one file, and whether it uses Temporal workflow/activity APIs."""
    wrapper = MetadataWrapper(cst.parse_module(source))
    rewriter = FileRewriter(path, project, options, diagnostics, import_table(wrapper.module))
    new_module = wrapper.visit(rewriter)
    if not rewriter.ctx.changed:
        return source, False
    return _fix_imports(new_module, rewriter.ctx).code, True

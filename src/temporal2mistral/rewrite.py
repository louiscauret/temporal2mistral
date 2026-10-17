"""Second pass: rewrite one file from the Temporal SDK to the Mistral Workflows SDK.

`FileRewriter` walks the syntax tree and hands each node it cares about to the
module that knows how to rewrite it:

- workflow classes and their methods, child workflows, continue-as-new: workflows.py
- activity definitions and calls: activities.py
- any other `workflow.X` / `activity.X`: temporal_api.py
- worker entry points and client code: workers.py, clients.py

Then `rewrite_file` fixes the imports of the result.
"""

from __future__ import annotations

from pathlib import Path

import libcst as cst
from libcst.codemod.visitors import AddImportsVisitor, RemoveImportsVisitor
from libcst.metadata import MetadataWrapper, ParentNodeProvider, PositionProvider

from .activities import rewrite_activity_call, rewrite_activity_definition
from .clients import plan_client_conversion
from .context import TEMPORAL_ACTIVITY_ALIAS, TEMPORAL_WORKFLOW_ALIAS, FileContext, Options
from .diagnostics import Diagnostics
from .scan import ACTIVITY_CALL_APIS, METHOD_ACTIVITY_CALL_APIS, Project, Workflow
from .syntax import code_of, dotted_name, full_name, import_table, split_decorator
from .temporal_api import ACTIVITY_MODULE, WORKFLOW_MODULE, rewrite_module_reference, rewrite_temporal_attribute
from .workers import WORKER, plan_worker_conversions
from .workflows import (
    params_model_code,
    rewrite_child_workflow_call,
    rewrite_continue_as_new,
    rewrite_workflow_decorator,
    rewrite_workflow_method,
)

# Activities given as a callable class instance: not supported by Mistral
CLASS_BASED_ACTIVITY_APIS = {"temporalio.workflow.execute_activity_class", "temporalio.workflow.start_activity_class"}
CHILD_WORKFLOW_APIS = {"temporalio.workflow.execute_child_workflow", "temporalio.workflow.start_child_workflow"}

# Imports of these modules are removed once the rewritten code no longer uses them.
TEMPORAL_MODULES_TO_CLEAN = {
    "temporalio.workflow",
    "temporalio.activity",
    "temporalio.common",
    "temporalio.client",
    "temporalio.worker",
}


class FileRewriter(cst.CSTTransformer):
    def __init__(self, ctx: FileContext) -> None:
        super().__init__()
        self.ctx = ctx
        # Classes we are in, innermost last, with the workflow each one defines (or None)
        self.class_stack: list[tuple[cst.ClassDef, Workflow | None]] = []
        self.function_depth = 0  # number of functions we are in
        self.module_references: set[cst.Name] = set()  # see _ModuleReferences

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

    # `workflow.X` and `activity.X`, or `workflow` and `activity` alone

    def visit_Module(self, node: cst.Module) -> None:
        references = _ModuleReferences(self.ctx.imports)
        node.visit(references)
        self.module_references = references.found

    def leave_Attribute(self, original_node: cst.Attribute, updated_node: cst.Attribute) -> cst.BaseExpression:
        if original_node in self.ctx.replacements:
            return cst.parse_expression(self.ctx.replacements[original_node])
        return rewrite_temporal_attribute(self.ctx, original_node, updated_node)

    def leave_Name(self, original_node: cst.Name, updated_node: cst.Name) -> cst.Name:
        if original_node in self.module_references:
            return rewrite_module_reference(self.ctx, original_node)
        return updated_node

    # Classes

    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        workflow = None
        for decorator in node.decorators:
            target, _ = split_decorator(decorator)
            if self._is_temporal(target, "temporalio.workflow.defn"):
                workflow = self.ctx.project.workflow_defined_at(str(self.ctx.path), node.name.value)
        self.class_stack.append((node, workflow))

    def leave_ClassDef(
        self, original_node: cst.ClassDef, updated_node: cst.ClassDef
    ) -> cst.ClassDef | cst.FlattenSentinel:
        _, workflow = self.class_stack.pop()
        if workflow is not None:
            decorators = []
            for original_decorator, decorator in zip(original_node.decorators, updated_node.decorators):
                target, _ = split_decorator(original_decorator)
                if self._is_temporal(target, "temporalio.workflow.defn"):
                    decorator = rewrite_workflow_decorator(self.ctx, original_node, original_decorator, decorator)
                decorators.append(decorator)
            updated_node = updated_node.with_changes(decorators=decorators)

        # Module-level activities replacing the activity methods of this class (see activities.py)
        functions = self.ctx.functions_after_class.pop(original_node.name.value, [])
        if not functions or self.class_stack:
            return updated_node
        spaced = [function.with_changes(leading_lines=[cst.EmptyLine(), cst.EmptyLine()]) for function in functions]
        return cst.FlattenSentinel([updated_node, *spaced])

    def _current_workflow(self) -> Workflow | None:
        for _, workflow in reversed(self.class_stack):
            if workflow is not None:
                return workflow
        return None

    # Functions and methods

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        self.function_depth += 1
        if node.asynchronous is not None:
            plan_worker_conversions(self.ctx, node)
            plan_client_conversion(self.ctx, node)

    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.FunctionDef | cst.FlattenSentinel:
        self.function_depth -= 1
        is_method = self._is_method(original_node)
        is_nested = self.function_depth > 0

        for index, decorator in enumerate(original_node.decorators):
            target, _ = split_decorator(decorator)
            if self._is_temporal(target, "temporalio.activity.defn"):
                return rewrite_activity_definition(self.ctx, original_node, updated_node, index, is_method, is_nested)

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

    # Statements and expressions replaced by workers.py and clients.py

    def leave_SimpleStatementLine(
        self, original_node: cst.SimpleStatementLine, updated_node: cst.SimpleStatementLine
    ) -> cst.BaseStatement | cst.FlattenSentinel | cst.RemovalSentinel:
        return self._replaced_statement(original_node, updated_node)

    def leave_With(
        self, original_node: cst.With, updated_node: cst.With
    ) -> cst.BaseStatement | cst.FlattenSentinel | cst.RemovalSentinel:
        return self._replaced_statement(original_node, updated_node)

    def _replaced_statement(
        self, original_node: cst.SimpleStatementLine | cst.With, updated_node: cst.BaseStatement
    ) -> cst.BaseStatement | cst.FlattenSentinel | cst.RemovalSentinel:
        if original_node not in self.ctx.replacements:
            return updated_node
        replacement = cst.parse_module(self.ctx.replacements[original_node])
        statements = list(replacement.body)
        if not statements:
            return cst.RemoveFromParent()
        # Comments before the first statement are parsed as the header of the replacement
        leading_lines = [*original_node.leading_lines, *replacement.header, *statements[0].leading_lines]
        statements[0] = statements[0].with_changes(leading_lines=leading_lines)
        return cst.FlattenSentinel(statements)

    def leave_Await(self, original_node: cst.Await, updated_node: cst.Await) -> cst.BaseExpression:
        if original_node in self.ctx.replacements:
            return cst.parse_expression(self.ctx.replacements[original_node])
        return updated_node

    # Calls

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.BaseExpression:
        api = self.ctx.full_name(original_node.func)
        if api is None:
            return updated_node
        short_name = api.split(".")[-1]

        if api in ACTIVITY_CALL_APIS:
            return rewrite_activity_call(self.ctx, original_node, updated_node, short_name)
        if api in METHOD_ACTIVITY_CALL_APIS:
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
        if original_node in self.ctx.converted_calls:
            return updated_node
        if api == WORKER:
            self._report_worker(original_node)
        elif api.startswith("temporalio.client.Client"):
            self.ctx.report(
                "T2M071",
                original_node,
                "Temporal client in use: starting, signalling and querying workflows goes through the Mistral API",
                "Use `Mistral(api_key=...).workflows.execute_workflow(workflow_identifier=..., input={...})` "
                "and `.workflows.executions.signal_workflow_execution(...)` / `.query_workflow_execution(...)`.",
                once_per_file=True,
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


class _ModuleReferences(cst.CSTVisitor):
    """Uses of the `workflow` or `activity` module itself, such as `api = workflow`.

    `workflow.X` is left to leave_Attribute, and keywords (`f(workflow=...)`) and parameters
    named `workflow` are not the module.
    """

    def __init__(self, imports: dict[str, str]) -> None:
        self.imports = imports
        self.found: set[cst.Name] = set()

    def visit_Import(self, node: cst.Import) -> bool:
        return False

    def visit_ImportFrom(self, node: cst.ImportFrom) -> bool:
        return False

    def visit_Attribute(self, node: cst.Attribute) -> bool:
        if not isinstance(node.value, cst.Name):
            node.value.visit(self)
        return False

    def visit_Arg(self, node: cst.Arg) -> bool:
        node.value.visit(self)
        return False

    def visit_Param(self, node: cst.Param) -> bool:
        if node.annotation is not None:
            node.annotation.visit(self)
        if node.default is not None:
            node.default.visit(self)
        return False

    def visit_Name(self, node: cst.Name) -> None:
        if full_name(node, self.imports) in (WORKFLOW_MODULE, ACTIVITY_MODULE):
            self.found.add(node)


def _fix_imports(module: cst.Module, ctx: FileContext) -> cst.Module:
    """Add the imports the rewritten code uses, and remove the Temporal imports it no longer uses."""
    used = _UsedNames()
    module.visit(used)
    for import_module, name, alias in sorted(ctx.needed_imports, key=str):
        bound_name = alias or name or import_module.split(".")[0]
        if bound_name in used.names:
            # `.activities` is a relative import: AddImportsVisitor takes the dots apart
            dots = len(import_module) - len(import_module.lstrip("."))
            AddImportsVisitor.add_needed_import(
                ctx.codemod_context, import_module.lstrip("."), name, alias, relative=dots
            )
    module = RemoveImportsVisitor(ctx.codemod_context).transform_module(module)
    return AddImportsVisitor(ctx.codemod_context).transform_module(module)


def rewrite_file(source: str, path: Path, project: Project, options: Options, diagnostics: Diagnostics) -> str:
    """Return the converted source of one file (the source itself when it uses no workflow or activity API)."""
    wrapper = MetadataWrapper(cst.parse_module(source))
    positions = wrapper.resolve(PositionProvider)
    parents = wrapper.resolve(ParentNodeProvider)
    ctx = FileContext(path, project, options, diagnostics, import_table(wrapper.module), positions, parents)
    new_module = wrapper.visit(FileRewriter(ctx))
    if not ctx.changed:
        return source
    return _fix_imports(new_module, ctx).code

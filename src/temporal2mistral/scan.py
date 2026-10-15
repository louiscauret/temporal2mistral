"""First pass: find the workflows, activities and activity calls of the whole project.

Mistral declares activity timeouts and retries on the activity definition, while
Temporal passes them at every call, often from another file. Scanning every file
first lets the second pass (rewrite.py) move the options to the right place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import libcst as cst
from libcst.metadata import MetadataWrapper, PositionProvider

from .syntax import (
    code_of,
    dotted_name,
    full_name,
    import_table,
    is_literal,
    keyword_args,
    positional_args,
    split_decorator,
    string_value,
)

# Temporal APIs that run a function-based activity
ACTIVITY_CALL_APIS = {
    "temporalio.workflow.execute_activity",
    "temporalio.workflow.start_activity",
    "temporalio.workflow.execute_local_activity",
    "temporalio.workflow.start_local_activity",
}


@dataclass
class ActivityCall:
    """One `workflow.execute_activity(...)` call and its options, as source code."""

    path: str
    line: int
    options: dict[str, str]  # {"start_to_close_timeout": "timedelta(seconds=10)", ...}
    retry_policy: dict[str, str] | None  # fields of `retry_policy=RetryPolicy(...)`, None without retry_policy
    retry_policy_code: str | None  # the `retry_policy=` value when it is not a literal RetryPolicy(...)


@dataclass
class Activity:
    function: str  # Python function name
    name: str  # Temporal activity name (`@activity.defn(name=...)`, defaults to the function name)
    path: str
    module: str  # dotted module name relative to the project root
    is_async: bool
    is_method: bool
    calls: list[ActivityCall] = field(default_factory=list)


@dataclass
class Parameter:
    name: str
    annotation: str | None
    default: str | None
    default_is_literal: bool


@dataclass
class Workflow:
    class_name: str
    name: str  # Temporal workflow name (`@workflow.defn(name=...)`, defaults to the class name)
    path: str
    parameters: list[Parameter]  # parameters of the `@workflow.run` method, without `self`


@dataclass
class _UnresolvedCall:
    call: ActivityCall
    target: str  # full name of the called function, or the activity name for a string target
    by_activity_name: bool


@dataclass
class Project:
    root: Path
    activities: list[Activity] = field(default_factory=list)
    workflows: list[Workflow] = field(default_factory=list)
    pydantic_models: set[str] = field(default_factory=set)  # class names deriving from BaseModel
    plain_classes: set[str] = field(default_factory=set)  # classes without decorator nor base class
    _unresolved_calls: list[_UnresolvedCall] = field(default_factory=list)

    def activity_defined_at(self, path: str, function: str) -> Activity | None:
        for activity in self.activities:
            if activity.path == path and activity.function == function:
                return activity
        return None

    def workflow_defined_at(self, path: str, class_name: str) -> Workflow | None:
        for workflow in self.workflows:
            if workflow.path == path and workflow.class_name == class_name:
                return workflow
        return None

    def workflow_named(self, class_name: str) -> Workflow | None:
        """The workflow class with this name, if there is exactly one."""
        found = [w for w in self.workflows if w.class_name == class_name]
        return found[0] if len(found) == 1 else None

    def workflow_with_temporal_name(self, name: str) -> Workflow | None:
        found = [w for w in self.workflows if w.name == name]
        return found[0] if len(found) == 1 else None

    def find_called_activities(self, target: str, caller_path: str, by_activity_name: bool = False) -> list[Activity]:
        """Activities a call refers to.

        ``target`` is the full name of the called function (``compose_greeting``,
        ``app.activities.charge``, ``.activities.charge``) or, with
        ``by_activity_name``, the Temporal activity name given as a string.
        """
        if by_activity_name:
            candidates = [a for a in self.activities if a.name == target]
            same_file = [a for a in candidates if a.path == caller_path]
            return same_file or candidates

        module, _, function = target.rpartition(".")
        candidates = [a for a in self.activities if a.function == function]
        if not module:
            # A plain name that is not imported: the activity is defined in the caller's file.
            return [a for a in candidates if a.path == caller_path]

        module = module.lstrip(".")
        matching = [
            a
            for a in candidates
            if a.module == module or a.module.endswith("." + module) or module.endswith("." + a.module)
        ]
        if matching:
            return matching
        # e.g. `import activities` from a sub-directory: accept the name if it is unique
        return candidates if len(candidates) == 1 else []

    def link_activity_calls(self) -> None:
        """Attach every recorded call to the activities it refers to. Run after scanning all files."""
        for unresolved in self._unresolved_calls:
            for activity in self.find_called_activities(
                unresolved.target, unresolved.call.path, unresolved.by_activity_name
            ):
                activity.calls.append(unresolved.call)
        self._unresolved_calls = []


def module_name(root: Path, path: Path) -> str:
    """``root/app/activities.py`` -> ``app.activities``."""
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        relative = Path(path.name)
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def workflow_parameters(run_method: cst.FunctionDef) -> list[Parameter]:
    params = run_method.params
    all_params = list(params.posonly_params) + list(params.params)
    all_params = all_params[1:] + list(params.kwonly_params)  # drop `self`
    result = []
    for param in all_params:
        result.append(
            Parameter(
                name=param.name.value,
                annotation=code_of(param.annotation.annotation) if param.annotation else None,
                default=code_of(param.default) if param.default is not None else None,
                default_is_literal=param.default is not None and is_literal(param.default),
            )
        )
    return result


class _Scanner(cst.CSTVisitor):
    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(
        self, project: Project, path: Path, class_bases: dict[str, set[str]], class_is_plain: dict[str, list[bool]]
    ) -> None:
        super().__init__()
        self.project = project
        self.path = str(path)
        self.module = module_name(project.root, path)
        self.class_bases = class_bases
        self.class_is_plain = class_is_plain
        self.imports: dict[str, str] = {}
        self.constants: dict[str, cst.BaseExpression] = {}
        self.class_depth = 0
        self.function_depth = 0

    def visit_Module(self, node: cst.Module) -> None:
        self.imports = import_table(node)
        # Module-level constants such as `RETRY = RetryPolicy(...)`, used when an option is passed by name.
        for statement in node.body:
            if not isinstance(statement, cst.SimpleStatementLine):
                continue
            for small in statement.body:
                if isinstance(small, cst.Assign) and len(small.targets) == 1:
                    target = small.targets[0].target
                    if isinstance(target, cst.Name):
                        self.constants[target.value] = small.value

    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        bases = set()
        for base in node.bases:
            name = dotted_name(base.value)
            if name:
                bases.add(name.split(".")[-1])
        self.class_bases.setdefault(node.name.value, set()).update(bases)
        is_plain = not node.decorators and bases <= {"object"} and self.class_depth == 0 and self.function_depth == 0
        self.class_is_plain.setdefault(node.name.value, []).append(is_plain)

        for decorator in node.decorators:
            target, call = split_decorator(decorator)
            if full_name(target, self.imports) == "temporalio.workflow.defn":
                self._add_workflow(node, call)
        self.class_depth += 1

    def leave_ClassDef(self, original_node: cst.ClassDef) -> None:
        self.class_depth -= 1

    def _add_workflow(self, node: cst.ClassDef, decorator_call: cst.Call | None) -> None:
        name = node.name.value
        name_arg = keyword_args(decorator_call).get("name")
        if name_arg is not None:
            name = string_value(name_arg.value) or name
        parameters: list[Parameter] = []
        for statement in node.body.body:
            if not isinstance(statement, cst.FunctionDef):
                continue
            for decorator in statement.decorators:
                target, _ = split_decorator(decorator)
                if full_name(target, self.imports) == "temporalio.workflow.run":
                    parameters = workflow_parameters(statement)
        self.project.workflows.append(Workflow(node.name.value, name, self.path, parameters))

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        for decorator in node.decorators:
            target, call = split_decorator(decorator)
            if full_name(target, self.imports) != "temporalio.activity.defn":
                continue
            name = node.name.value
            name_arg = keyword_args(call).get("name")
            if name_arg is not None:
                name = string_value(name_arg.value) or name
            activity = Activity(
                function=node.name.value,
                name=name,
                path=self.path,
                module=self.module,
                is_async=node.asynchronous is not None,
                is_method=self.class_depth > 0 and self.function_depth == 0,
            )
            self.project.activities.append(activity)
        self.function_depth += 1

    def leave_FunctionDef(self, original_node: cst.FunctionDef) -> None:
        self.function_depth -= 1

    def visit_Call(self, node: cst.Call) -> None:
        if full_name(node.func, self.imports) not in ACTIVITY_CALL_APIS:
            return
        positional = positional_args(node)
        keywords = keyword_args(node)
        if positional:
            target = positional[0].value
        elif "activity" in keywords:
            target = keywords["activity"].value
        else:
            return

        options = {}
        for keyword, arg in keywords.items():
            if keyword not in ("activity", "arg", "args", "retry_policy"):
                options[keyword] = code_of(self._resolve_constant(arg.value))
        retry_policy, retry_policy_code = None, None
        if "retry_policy" in keywords:
            retry_policy, retry_policy_code = self._read_retry_policy(keywords["retry_policy"].value)

        line = self.get_metadata(PositionProvider, node).start.line
        call = ActivityCall(self.path, line, options, retry_policy, retry_policy_code)

        activity_name = string_value(target)
        if activity_name is not None:
            self.project._unresolved_calls.append(_UnresolvedCall(call, activity_name, by_activity_name=True))
            return
        target_name = full_name(target, self.imports)
        if target_name is not None:
            self.project._unresolved_calls.append(_UnresolvedCall(call, target_name, by_activity_name=False))

    def _resolve_constant(self, value: cst.BaseExpression) -> cst.BaseExpression:
        if isinstance(value, cst.Name) and value.value in self.constants:
            return self.constants[value.value]
        return value

    def _read_retry_policy(self, value: cst.BaseExpression) -> tuple[dict[str, str] | None, str | None]:
        """Fields of a literal ``RetryPolicy(...)``, or the source code when it is anything else."""
        value = self._resolve_constant(value)
        is_retry_policy = isinstance(value, cst.Call) and (dotted_name(value.func) or "").endswith("RetryPolicy")
        if not is_retry_policy or positional_args(value):
            return None, code_of(value)
        fields = {}
        for keyword, arg in keyword_args(value).items():
            fields[keyword] = code_of(self._resolve_constant(arg.value))
        return fields, None


def scan_project(root: Path, modules: dict[Path, cst.Module]) -> Project:
    project = Project(root=root)
    class_bases: dict[str, set[str]] = {}
    class_is_plain: dict[str, list[bool]] = {}
    for path, module in modules.items():
        MetadataWrapper(module, unsafe_skip_copy=True).visit(_Scanner(project, path, class_bases, class_is_plain))
    project.link_activity_calls()

    # A class is a pydantic model if it derives from BaseModel, directly or through other project classes.
    models = {"BaseModel"}
    found_new_model = True
    while found_new_model:
        found_new_model = False
        for class_name, bases in class_bases.items():
            if class_name not in models and bases & models:
                models.add(class_name)
                found_new_model = True
    project.pydantic_models = models - {"BaseModel"}

    # Plain classes have no JSON schema; only flag a name when all its definitions are plain.
    project.plain_classes = {name for name, plain in class_is_plain.items() if all(plain)}
    return project

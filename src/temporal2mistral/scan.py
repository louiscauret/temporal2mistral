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

from .calls import read_temporal_call
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
# Temporal APIs that run an activity defined as a method: `execute_activity_method(MyActivities.do, ...)`
METHOD_ACTIVITY_CALL_APIS = {
    "temporalio.workflow.execute_activity_method",
    "temporalio.workflow.start_activity_method",
    "temporalio.workflow.execute_local_activity_method",
    "temporalio.workflow.start_local_activity_method",
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
    is_method: bool
    is_nested: bool  # defined inside a function
    class_name: str | None = None  # for a method: its class
    becomes_function: bool = False  # a method that activities.py turns into a module-level function
    calls: list[ActivityCall] = field(default_factory=list)


@dataclass
class ClassDefinition:
    name: str
    path: str
    module: str
    is_plain: bool  # no decorator and no base class: Mistral cannot build a JSON schema for it


@dataclass
class Parameter:
    name: str
    annotation: str | None
    default: str | None
    default_is_literal: bool


@dataclass
class Handler:
    """A signal, query or update handler of a workflow."""

    name: str  # the name clients use (`@workflow.signal(name=...)`, defaults to the method name)
    parameters: list[Parameter]


@dataclass
class Workflow:
    class_name: str
    name: str  # Temporal workflow name (`@workflow.defn(name=...)`, defaults to the class name)
    path: str
    parameters: list[Parameter]  # parameters of the `@workflow.run` method, without `self`
    handlers: dict[str, Handler] = field(default_factory=dict)  # by method name


@dataclass
class Project:
    root: Path
    activities: list[Activity] = field(default_factory=list)
    workflows: list[Workflow] = field(default_factory=list)
    pydantic_models: set[str] = field(default_factory=set)  # class names deriving from BaseModel
    classes: list[ClassDefinition] = field(default_factory=list)

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
        return _narrow(candidates, module, caller_path)

    def find_method_activities(self, target: str, caller_path: str) -> list[Activity]:
        """Activities a method call refers to; ``target`` is ``MyActivities.do`` or ``app.acts.MyActivities.do``."""
        class_target, _, method = target.rpartition(".")
        module, _, class_name = class_target.rpartition(".")
        candidates = [a for a in self.activities if a.class_name == class_name and a.function == method]
        return _narrow(candidates, module, caller_path)

    def is_plain_class(self, name: str, used_in: str, imported_from: str | None) -> bool:
        """Is the class ``name``, used in the file ``used_in``, a plain class?

        ``imported_from`` is what that file imports the name from (``models.Input``), or None.
        Projects often define classes with the same name in several places, so the definition
        is looked up where the file imports it from, or in the file itself.
        """
        candidates = [c for c in self.classes if c.name == name]
        if imported_from is not None:
            module = imported_from.rpartition(".")[0]
            found = [c for c in candidates if _same_module(c.module, module)]
        else:
            found = [c for c in candidates if c.path == used_in]
        if found:
            candidates = found
        # Without a definition to look at, only flag a name when all its definitions are plain.
        return bool(candidates) and all(c.is_plain for c in candidates)


def _narrow(candidates: list[Activity], module: str, caller_path: str) -> list[Activity]:
    """The activities a call can refer to, knowing the module it imports them from ("" when not imported)."""
    if not module:
        # A plain name that is not imported: the activity is defined in the caller's file.
        return [a for a in candidates if a.path == caller_path]
    matching = [a for a in candidates if _same_module(a.module, module)]
    if matching:
        return matching
    # e.g. `import activities` from a sub-directory: accept the name if it is unique
    return candidates if len(candidates) == 1 else []


def _same_module(defined_in: str, imported_from: str) -> bool:
    """Can ``imported_from`` (``app.models``, ``.models``, ``models``) be the module ``defined_in``?

    Module names are relative to the project root, which is not always what the code imports from.
    """
    imported_from = imported_from.lstrip(".")
    return (
        defined_in == imported_from
        or defined_in.endswith("." + imported_from)
        or imported_from.endswith("." + defined_in)
    )


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


def method_parameters(method: cst.FunctionDef) -> list[Parameter]:
    params = method.params
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
    """Visits every file of the project in turn (call `start_file` before each one)."""

    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(self, project: Project) -> None:
        super().__init__()
        self.project = project
        # Collected across all files, used once every file has been scanned:
        self.class_bases: dict[str, set[str]] = {}  # class name -> names of its base classes
        # Activity calls with their target and how to look it up: "function" (full name of the
        # function), "name" (Temporal activity name) or "method" (`MyActivities.do`).
        # Linked to activities once every activity is known.
        self.calls: list[tuple[ActivityCall, str, str]] = []

    def start_file(self, path: Path) -> None:
        self.path = str(path)
        self.module = module_name(self.project.root, path)
        self.imports: dict[str, str] = {}
        self.constants: dict[str, cst.BaseExpression] = {}
        self.class_stack: list[cst.ClassDef] = []  # classes we are in, innermost last
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
        is_top_level = not self.class_stack and self.function_depth == 0
        is_plain = not node.decorators and bases <= {"object"} and is_top_level
        self.project.classes.append(ClassDefinition(node.name.value, self.path, self.module, is_plain))

        for decorator in node.decorators:
            target, call = split_decorator(decorator)
            if full_name(target, self.imports) == "temporalio.workflow.defn":
                self._add_workflow(node, call)
        self.class_stack.append(node)

    def leave_ClassDef(self, original_node: cst.ClassDef) -> None:
        self.class_stack.pop()

    def _add_workflow(self, node: cst.ClassDef, decorator_call: cst.Call | None) -> None:
        name = node.name.value
        name_arg = keyword_args(decorator_call).get("name")
        if name_arg is not None:
            name = string_value(name_arg.value) or name
        workflow = Workflow(node.name.value, name, self.path, parameters=[])
        for statement in node.body.body:
            if not isinstance(statement, cst.FunctionDef):
                continue
            for decorator in statement.decorators:
                target, call = split_decorator(decorator)
                kind = full_name(target, self.imports)
                if kind == "temporalio.workflow.run":
                    workflow.parameters = method_parameters(statement)
                elif kind in ("temporalio.workflow.signal", "temporalio.workflow.query", "temporalio.workflow.update"):
                    workflow.handlers[statement.name.value] = Handler(
                        _handler_name(statement, call), method_parameters(statement)
                    )
        self.project.workflows.append(workflow)

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
                is_method=bool(self.class_stack) and self.function_depth == 0,
                is_nested=self.function_depth > 0,
            )
            if activity.is_method:
                cls = self.class_stack[-1]
                activity.class_name = cls.name.value
                # `Depends(MyActivities)` builds the instance, the new function goes next to the (top-level)
                # class, and its instance parameter cannot come after `**kwargs`.
                activity.becomes_function = (
                    len(self.class_stack) == 1 and _needs_no_arguments(cls) and node.params.star_kwarg is None
                )
            self.project.activities.append(activity)
        self.function_depth += 1

    def leave_FunctionDef(self, original_node: cst.FunctionDef) -> None:
        self.function_depth -= 1

    def visit_Call(self, node: cst.Call) -> None:
        api = full_name(node.func, self.imports)
        if api not in ACTIVITY_CALL_APIS and api not in METHOD_ACTIVITY_CALL_APIS:
            return
        temporal_call = read_temporal_call(node, target_keyword="activity")
        if temporal_call.target is None:
            return

        options = {}
        for keyword, arg in temporal_call.options.items():
            if keyword != "retry_policy":
                options[keyword] = code_of(self._resolve_constant(arg.value))
        retry_policy, retry_policy_code = None, None
        if "retry_policy" in temporal_call.options:
            retry_policy, retry_policy_code = self._read_retry_policy(temporal_call.options["retry_policy"].value)

        line = self.get_metadata(PositionProvider, node).start.line
        call = ActivityCall(self.path, line, options, retry_policy, retry_policy_code)

        activity_name = string_value(temporal_call.target)
        target_name = full_name(temporal_call.target, self.imports)
        if activity_name is not None:
            self.calls.append((call, activity_name, "name"))
        elif target_name is not None and api in METHOD_ACTIVITY_CALL_APIS:
            self.calls.append((call, target_name, "method"))
        elif target_name is not None:
            self.calls.append((call, target_name, "function"))

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


def _handler_name(method: cst.FunctionDef, decorator_call: cst.Call | None) -> str:
    """`@workflow.signal`, `@workflow.signal("name")` or `@workflow.signal(name="name")`."""
    if decorator_call is not None and positional_args(decorator_call):
        return string_value(positional_args(decorator_call)[0].value) or method.name.value
    name_arg = keyword_args(decorator_call).get("name")
    if name_arg is not None:
        return string_value(name_arg.value) or method.name.value
    return method.name.value


def _needs_no_arguments(cls: cst.ClassDef) -> bool:
    """Can `cls()` be called without arguments? Only answered for classes without base classes."""
    if any((dotted_name(base.value) or "") != "object" for base in cls.bases):
        return False
    for statement in cls.body.body:
        if isinstance(statement, cst.FunctionDef) and statement.name.value == "__init__":
            return all(parameter.default is not None for parameter in method_parameters(statement))
    return True


def scan_project(root: Path, modules: dict[Path, cst.Module]) -> Project:
    project = Project(root=root)
    scanner = _Scanner(project)
    for path, module in modules.items():
        scanner.start_file(path)
        MetadataWrapper(module, unsafe_skip_copy=True).visit(scanner)

    # Calls are linked only now: an activity may be defined in a file scanned after its callers.
    for call, target, lookup in scanner.calls:
        if lookup == "method":
            found = project.find_method_activities(target, call.path)
        else:
            found = project.find_called_activities(target, call.path, by_activity_name=lookup == "name")
        for activity in found:
            activity.calls.append(call)

    # Two activities of a file with the same function name would give two functions with that name.
    for activity in project.activities:
        if activity.becomes_function:
            same_name = [a for a in project.activities if a.path == activity.path and a.function == activity.function]
            activity.becomes_function = len(same_name) == 1

    # A class is a pydantic model if it derives from BaseModel, directly or through other project classes.
    models = {"BaseModel"}
    found_new_model = True
    while found_new_model:
        found_new_model = False
        for class_name, bases in scanner.class_bases.items():
            if class_name not in models and bases & models:
                models.add(class_name)
                found_new_model = True
    project.pydantic_models = models - {"BaseModel"}
    return project

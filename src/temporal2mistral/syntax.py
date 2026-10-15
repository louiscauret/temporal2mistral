"""Small helpers to read LibCST nodes and to resolve names through a module's imports."""

from __future__ import annotations

import libcst as cst


def code_of(node: cst.CSTNode) -> str:
    """Source code of a node, e.g. ``timedelta(seconds=10)``."""
    return cst.Module(body=[]).code_for_node(node)


def dotted_name(node: cst.BaseExpression) -> str | None:
    """``a.b.c`` -> ``"a.b.c"``. Anything else (calls, subscripts...) -> None."""
    if isinstance(node, cst.Name):
        return node.value
    if isinstance(node, cst.Attribute):
        base = dotted_name(node.value)
        if base is not None:
            return f"{base}.{node.attr.value}"
    return None


def string_value(node: cst.BaseExpression) -> str | None:
    """Value of a string literal such as ``"my-activity"``, else None."""
    if isinstance(node, cst.SimpleString):
        value = node.evaluated_value
        if isinstance(value, str):
            return value
    return None


def positional_args(call: cst.Call) -> list[cst.Arg]:
    return [arg for arg in call.args if arg.keyword is None and not arg.star]


def keyword_args(call: cst.Call | None) -> dict[str, cst.Arg]:
    if call is None:
        return {}
    return {arg.keyword.value: arg for arg in call.args if arg.keyword is not None}


def split_decorator(decorator: cst.Decorator) -> tuple[cst.BaseExpression, cst.Call | None]:
    """``@x`` -> (x, None) and ``@x(...)`` -> (x, the call)."""
    expression = decorator.decorator
    if isinstance(expression, cst.Call):
        return expression.func, expression
    return expression, None


def is_literal(node: cst.BaseExpression) -> bool:
    """True for values that can be copied anywhere: numbers, strings, None, True, False."""
    if isinstance(node, (cst.Integer, cst.Float, cst.SimpleString, cst.ConcatenatedString)):
        return True
    if isinstance(node, cst.Name) and node.value in ("None", "True", "False"):
        return True
    if isinstance(node, cst.UnaryOperation) and isinstance(node.expression, (cst.Integer, cst.Float)):
        return True
    return False


class _ImportCollector(cst.CSTVisitor):
    def __init__(self) -> None:
        self.names: dict[str, str] = {}

    def visit_Import(self, node: cst.Import) -> None:
        for alias in node.names:
            full = dotted_name(alias.name)
            if full is None:
                continue
            if alias.asname is not None:
                self.names[alias.asname.name.value] = full
            else:
                # `import a.b.c` binds the name `a`
                first = full.split(".")[0]
                self.names[first] = first

    def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
        if isinstance(node.names, cst.ImportStar):
            return
        dots = "." * len(node.relative)  # relative imports keep their dots: `.activities`
        module = dotted_name(node.module) if node.module is not None else None
        for alias in node.names:
            name = dotted_name(alias.name)
            if name is None:
                continue
            local = alias.asname.name.value if alias.asname is not None else name
            if module:
                self.names[local] = f"{dots}{module}.{name}"
            else:
                self.names[local] = f"{dots}{name}"


def import_table(module: cst.Module) -> dict[str, str]:
    """Map every name bound by an import of the module to what it refers to.

    ``from temporalio import workflow`` gives ``{"workflow": "temporalio.workflow"}``.
    Imports are collected anywhere in the module, including inside
    ``with workflow.unsafe.imports_passed_through():`` blocks. Local variables that
    shadow an imported name are not tracked: the tool assumes they do not exist.
    """
    collector = _ImportCollector()
    module.visit(collector)
    return collector.names


def full_name(node: cst.BaseExpression, imports: dict[str, str]) -> str | None:
    """Resolve ``workflow.defn`` to ``temporalio.workflow.defn`` using the import table."""
    name = dotted_name(node)
    if name is None:
        return None
    first, _, rest = name.partition(".")
    base = imports.get(first, first)
    return f"{base}.{rest}" if rest else base

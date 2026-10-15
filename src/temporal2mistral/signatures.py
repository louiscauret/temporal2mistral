"""Check (and when safe, fix) the signatures Mistral turns into JSON schemas.

Mistral builds a pydantic model from the parameters and the return type of every
entrypoint, handler and activity, so they all need annotations it can handle.
"""

from __future__ import annotations

import libcst as cst

from .context import FileContext
from .syntax import dotted_name

NONE_ANNOTATION = cst.Annotation(
    annotation=cst.Name("None"),
    whitespace_before_indicator=cst.SimpleWhitespace(" "),
    whitespace_after_indicator=cst.SimpleWhitespace(" "),
)


class _ReturnsValue(cst.CSTVisitor):
    """Does the body of a function `return <something>` (other than None) or `yield`?

    Nested functions, lambdas and classes are skipped: their returns are not ours.
    """

    def __init__(self) -> None:
        self.found = False

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        return False

    def visit_Lambda(self, node: cst.Lambda) -> bool:
        return False

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        return False

    def visit_Return(self, node: cst.Return) -> None:
        returns_none = isinstance(node.value, cst.Name) and node.value.value == "None"
        if node.value is not None and not returns_none:
            self.found = True

    def visit_Yield(self, node: cst.Yield) -> None:
        self.found = True


class _TypeNames(cst.CSTVisitor):
    """Names used in an annotation: `Optional[Foo]` -> {"Optional", "Foo"}."""

    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Name(self, node: cst.Name) -> None:
        self.names.add(node.value)

    def visit_Attribute(self, node: cst.Attribute) -> bool:
        self.names.add(node.attr.value)
        return False


def returns_a_value(function: cst.FunctionDef) -> bool:
    visitor = _ReturnsValue()
    function.body.visit(visitor)
    return visitor.found


def check_signature(
    ctx: FileContext,
    original: cst.FunctionDef,
    updated: cst.FunctionDef,
    kind: str,
    is_method: bool,
) -> cst.FunctionDef:
    """Report missing or unsupported annotations; add `-> None` or replace `-> NoReturn` when that is safe.

    ``kind`` is "entrypoint", "signal", "query", "update" or "activity".
    """
    name = original.name.value
    params = list(original.params.posonly_params) + list(original.params.params)
    if is_method:
        params = params[1:]  # self
    params += list(original.params.kwonly_params)

    missing = [param.name.value for param in params if param.annotation is None]
    if isinstance(original.params.star_arg, cst.Param):
        missing.append("*" + original.params.star_arg.name.value)
    if missing:
        ctx.report(
            "T2M002",
            original,
            f"`{name}` has parameters without type annotation: {', '.join(missing)}",
            "Mistral builds the input schema from the annotations: annotate every parameter.",
        )
    for param in params:
        if param.annotation is not None:
            _check_schema_types(
                ctx, original, param.annotation.annotation, f"parameter `{param.name.value}` of `{name}`"
            )

    # Mistral rejects queries and updates that return nothing.
    must_return_a_value = kind in ("query", "update")

    if original.returns is None:
        if returns_a_value(original):
            ctx.report(
                "T2M004",
                original,
                f"`{name}` returns a value but has no return annotation",
                "Add the return type (Mistral validates results against it).",
            )
        elif must_return_a_value:
            _report_handler_without_result(ctx, original, kind)
        else:
            ctx.report("T2M003", original, f"`-> None` added to `{name}`")
            updated = updated.with_changes(returns=NONE_ANNOTATION)
        return updated

    return_type = original.returns.annotation
    return_type_name = (dotted_name(return_type) or "").split(".")[-1]
    if return_type_name in ("NoReturn", "Never"):
        if must_return_a_value:
            _report_handler_without_result(ctx, original, kind)
            return updated
        ctx.report(
            "T2M005",
            original,
            f"`-> {return_type_name}` of `{name}` replaced by `-> None`",
            "Mistral builds a schema from the return type and cannot do it for NoReturn/Never.",
        )
        return updated.with_changes(returns=NONE_ANNOTATION)

    if must_return_a_value and return_type_name == "None":
        _report_handler_without_result(ctx, original, kind)
    else:
        _check_schema_types(ctx, original, return_type, f"return type of `{name}`")
    return updated


def _report_handler_without_result(ctx: FileContext, function: cst.FunctionDef, kind: str) -> None:
    ctx.report(
        "T2M015",
        function,
        f"{kind.capitalize()} handler `{function.name.value}` returns nothing; "
        f"Mistral requires {kind} handlers to return a value",
        "Return something meaningful (e.g. the new state, or a bool) and annotate it.",
    )


def _check_schema_types(
    ctx: FileContext, function: cst.FunctionDef, annotation: cst.BaseExpression, where: str
) -> None:
    visitor = _TypeNames()
    annotation.visit(visitor)
    for type_name in sorted(visitor.names & ctx.project.plain_classes):
        ctx.report(
            "T2M006",
            function,
            f"`{type_name}` ({where}) is a plain class: Mistral cannot build a JSON schema for it",
            "Make it a dataclass or a pydantic model (Temporal accepted it through a custom payload converter).",
        )

"""Read the arguments of Temporal calls such as `execute_activity` and `execute_child_workflow`."""

from __future__ import annotations

from dataclasses import dataclass

import libcst as cst

from .syntax import code_of, keyword_args, positional_args


@dataclass
class TemporalCall:
    """`execute_activity(target, payload..., options...)` split into its parts."""

    target: cst.BaseExpression | None  # the activity or workflow
    payload: list[str]  # arguments for the activity or workflow, as source code
    payload_is_unpacked: bool  # True for `args=some_list`: it becomes `*some_list`
    options: dict[str, cst.Arg]  # every other keyword argument


def read_temporal_call(call: cst.Call, target_keyword: str | None) -> TemporalCall:
    """Temporal accepts the target positionally or as a keyword (`activity=`, `workflow=`),
    and the payload as extra positional arguments, `arg=x` or `args=[a, b]`.

    Use ``target_keyword=None`` for calls without a target, such as `continue_as_new(x)`.
    """
    positional = positional_args(call)
    keywords = keyword_args(call)

    target = None
    extra_positional = positional
    if target_keyword is not None:
        if positional:
            target = positional[0].value
            extra_positional = positional[1:]
        elif target_keyword in keywords:
            target = keywords[target_keyword].value

    payload = [code_of(arg.value) for arg in extra_positional]
    unpacked = False
    if not payload and "arg" in keywords:
        payload = [code_of(keywords["arg"].value)]
    elif not payload and "args" in keywords:
        args_value = keywords["args"].value
        if isinstance(args_value, (cst.List, cst.Tuple)):
            payload = [code_of(element.value) for element in args_value.elements]
        else:
            payload = ["*" + code_of(args_value)]
            unpacked = True

    options = {name: arg for name, arg in keywords.items() if name not in (target_keyword, "arg", "args")}
    return TemporalCall(target, payload, unpacked, options)

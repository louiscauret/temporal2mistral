"""Read and write the literal values used by activity options.

Options are moved from a call site to an activity definition that may live in
another module, so only self-contained values are moved: numbers and
``timedelta(...)`` calls with number arguments.
"""

from __future__ import annotations

import ast

SECONDS_PER_UNIT = {
    "weeks": 7 * 24 * 3600,
    "days": 24 * 3600,
    "hours": 3600,
    "minutes": 60,
    "seconds": 1,
    "milliseconds": 0.001,
    "microseconds": 0.000001,
}

# Order of the positional arguments of datetime.timedelta
TIMEDELTA_POSITIONAL_UNITS = ["days", "seconds", "microseconds", "milliseconds", "minutes", "hours", "weeks"]


def _number(node: ast.expr) -> float | None:
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return float(node.value)
    return None


def parse_number(code: str) -> float | None:
    """``"3"`` -> 3.0; anything that is not a plain number -> None."""
    try:
        node = ast.parse(code, mode="eval").body
    except SyntaxError:
        return None
    return _number(node)


def parse_timedelta(code: str) -> float | None:
    """``"timedelta(minutes=2)"`` -> 120.0 seconds; anything else -> None."""
    try:
        node = ast.parse(code, mode="eval").body
    except SyntaxError:
        return None
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Name):
        function_name = node.func.id
    elif isinstance(node.func, ast.Attribute):
        function_name = node.func.attr  # datetime.timedelta(...)
    else:
        return None
    if function_name != "timedelta" or len(node.args) > len(TIMEDELTA_POSITIONAL_UNITS):
        return None

    seconds = 0.0
    for unit, argument in zip(TIMEDELTA_POSITIONAL_UNITS, node.args):
        value = _number(argument)
        if value is None:
            return None
        seconds += value * SECONDS_PER_UNIT[unit]
    for keyword in node.keywords:
        value = _number(keyword.value)
        if keyword.arg not in SECONDS_PER_UNIT or value is None:
            return None
        seconds += value * SECONDS_PER_UNIT[keyword.arg]
    return seconds


def timedelta_code(seconds: float) -> str:
    """120 -> ``"timedelta(minutes=2)"``, using the largest unit that divides exactly."""
    if seconds != int(seconds):
        return f"timedelta(seconds={seconds})"
    whole = int(seconds)
    for unit in ("days", "hours", "minutes"):
        size = SECONDS_PER_UNIT[unit]
        if whole and whole % size == 0:
            return f"timedelta({unit}={whole // size})"
    return f"timedelta(seconds={whole})"


def number_code(value: float) -> str:
    """3.0 -> ``"3"``, 1.5 -> ``"1.5"``."""
    if value == int(value):
        return str(int(value))
    return str(value)


def parse_cli_duration(text: str) -> float:
    """Durations given on the command line: ``30d``, ``12h``, ``90m``, ``45s``, ``1w`` or seconds."""
    units = {"w": "weeks", "d": "days", "h": "hours", "m": "minutes", "s": "seconds"}
    text = text.strip().lower()
    if text and text[-1] in units:
        return float(text[:-1]) * SECONDS_PER_UNIT[units[text[-1]]]
    return float(text)

"""Turn the options of every call to an activity into the arguments of its `@activity(...)` decorator.

Temporal: ``await workflow.execute_activity(charge, x, start_to_close_timeout=timedelta(minutes=2))``
Mistral:  ``@activity(start_to_close_timeout=timedelta(minutes=2))`` on ``charge``.

When the call sites disagree, the most permissive value wins (longest timeout,
most retries) so that no call fails sooner than it did on Temporal.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .durations import number_code, parse_number, parse_timedelta, timedelta_code
from .scan import ActivityCall

RETRY_FIELDS_KEPT = ("maximum_attempts", "backoff_coefficient")


@dataclass
class Note:
    """A diagnostic to report on the activity definition."""

    code: str
    message: str
    hint: str | None = None


@dataclass
class DecoratorOptions:
    arguments: list[str] = field(default_factory=list)  # e.g. ["start_to_close_timeout=timedelta(seconds=10)"]
    notes: list[Note] = field(default_factory=list)
    uses_timedelta: bool = False


def _where(call: ActivityCall) -> str:
    return f"{Path(call.path).name}:{call.line}"


def decorator_options(
    activity_function: str, calls: list[ActivityCall], keep_unlimited_retries: bool
) -> DecoratorOptions:
    result = DecoratorOptions()
    if not calls:
        result.notes.append(
            Note(
                "T2M025",
                f"No `execute_activity` call found for `{activity_function}`: Mistral defaults apply "
                "(start_to_close_timeout=5 minutes, 3 attempts)",
            )
        )
        return result

    _add_timeouts(activity_function, calls, result)
    _add_retries(activity_function, calls, result, keep_unlimited_retries)

    if result.arguments:
        moved = ", ".join(argument.split("=")[0] for argument in result.arguments)
        places = ", ".join(sorted({_where(call) for call in calls}))
        result.notes.append(Note("T2M020", f"{moved} moved to `@activity` on `{activity_function}` (from {places})"))
    return result


def _timeout_values(
    activity_function: str, calls: list[ActivityCall], option: str, result: DecoratorOptions
) -> dict[float, list[str]]:
    """Seconds -> call sites using that value, for one timeout option."""
    values: dict[float, list[str]] = {}
    for call in calls:
        code = call.options.get(option)
        if code is None:
            continue
        seconds = parse_timedelta(code)
        if seconds is None:
            result.notes.append(
                Note(
                    "T2M022",
                    f"`{option}={code}` at {_where(call)} is not a literal timedelta "
                    f"and was not moved to `{activity_function}`",
                    f"Set `{option}` on the `@activity(...)` decorator of `{activity_function}` yourself.",
                )
            )
            continue
        values.setdefault(seconds, []).append(_where(call))
    return values


def _report_disagreement(
    activity_function: str,
    option: str,
    values: dict[float, list[str]],
    kept: float,
    render: Callable[[float], str],  # how to show a value in the message, e.g. timedelta_code
    result: DecoratorOptions,
) -> None:
    if len(values) < 2:
        return
    details = ", ".join(f"{render(value)} at {', '.join(places)}" for value, places in sorted(values.items()))
    result.notes.append(
        Note("T2M021", f"Call sites of `{activity_function}` disagree on `{option}` ({details}); kept {render(kept)}")
    )


def _add_timeouts(activity_function: str, calls: list[ActivityCall], result: DecoratorOptions) -> None:
    start_to_close = _timeout_values(activity_function, calls, "start_to_close_timeout", result)
    schedule_to_close = _timeout_values(activity_function, calls, "schedule_to_close_timeout", result)

    # Mistral only has start_to_close_timeout.
    if schedule_to_close and not start_to_close:
        start_to_close = schedule_to_close
        result.notes.append(
            Note(
                "T2M023",
                f"`schedule_to_close_timeout` used as `start_to_close_timeout` for `{activity_function}` "
                "(Mistral only has the latter)",
            )
        )
    elif schedule_to_close:
        result.notes.append(
            Note(
                "T2M023",
                f"`schedule_to_close_timeout` dropped for `{activity_function}` "
                "(Mistral only has `start_to_close_timeout`)",
            )
        )
    if any("schedule_to_start_timeout" in call.options for call in calls):
        result.notes.append(Note("T2M023", f"`schedule_to_start_timeout` dropped for `{activity_function}`"))

    if start_to_close:
        _report_disagreement(
            activity_function, "start_to_close_timeout", start_to_close, max(start_to_close), timedelta_code, result
        )
        result.arguments.append(f"start_to_close_timeout={timedelta_code(max(start_to_close))}")
        result.uses_timedelta = True

    heartbeat = _timeout_values(activity_function, calls, "heartbeat_timeout", result)
    if heartbeat:
        _report_disagreement(activity_function, "heartbeat_timeout", heartbeat, max(heartbeat), timedelta_code, result)
        result.arguments.append(f"heartbeat_timeout={timedelta_code(max(heartbeat))}")
        result.uses_timedelta = True


def _add_retries(
    activity_function: str, calls: list[ActivityCall], result: DecoratorOptions, keep_unlimited_retries: bool
) -> None:
    # Temporal retries forever unless `maximum_attempts` is set; 0 means "unlimited" for both SDKs.
    attempts: dict[float, list[str]] = {}
    backoff: dict[float, list[str]] = {}
    dropped_fields: set[str] = set()

    for call in calls:
        if call.retry_policy_code is not None:
            result.notes.append(
                Note(
                    "T2M022",
                    f"`retry_policy={call.retry_policy_code}` at {_where(call)} is not a literal `RetryPolicy(...)` "
                    f"and was not moved to `{activity_function}`",
                    "Set `retry_policy_max_attempts` and `retry_policy_backoff_coefficient` "
                    "on `@activity(...)` yourself.",
                )
            )
            continue
        fields = call.retry_policy or {}

        if "maximum_attempts" in fields:
            value = parse_number(fields["maximum_attempts"])
        else:
            value = 0.0
        if value is None:
            result.notes.append(
                Note("T2M022", f"`maximum_attempts={fields['maximum_attempts']}` at {_where(call)} is not a literal")
            )
        else:
            attempts.setdefault(value, []).append(_where(call))

        if "backoff_coefficient" in fields:
            coefficient = parse_number(fields["backoff_coefficient"])
            if coefficient is None:
                result.notes.append(
                    Note(
                        "T2M022",
                        f"`backoff_coefficient={fields['backoff_coefficient']}` at {_where(call)} is not a literal",
                    )
                )
            else:
                backoff.setdefault(coefficient, []).append(_where(call))

        for name in fields:
            if name not in RETRY_FIELDS_KEPT:
                dropped_fields.add(name)

    for name in sorted(dropped_fields):
        result.notes.append(
            Note(
                "T2M024",
                f"`RetryPolicy({name}=...)` dropped for `{activity_function}`",
                "Mistral exposes `retry_policy_max_attempts` and `retry_policy_backoff_coefficient` only.",
            )
        )

    if attempts:
        kept = 0.0 if 0.0 in attempts else max(attempts)
        _report_disagreement(activity_function, "retry attempts", attempts, kept, _attempts_text, result)
        if kept == 0.0:
            if keep_unlimited_retries:
                result.arguments.append("retry_policy_max_attempts=0")
                result.notes.append(
                    Note(
                        "T2M026",
                        f"`retry_policy_max_attempts=0` (unlimited, Temporal's default) set on `{activity_function}`",
                        "Remove it to use Mistral's default of 3 attempts, or rerun with --mistral-retry-defaults.",
                    )
                )
        else:
            result.arguments.append(f"retry_policy_max_attempts={number_code(kept)}")

    if backoff:
        _report_disagreement(activity_function, "backoff_coefficient", backoff, max(backoff), number_code, result)
        result.arguments.append(f"retry_policy_backoff_coefficient={number_code(max(backoff))}")


def _attempts_text(value: float) -> str:
    return "unlimited" if value == 0 else number_code(value)

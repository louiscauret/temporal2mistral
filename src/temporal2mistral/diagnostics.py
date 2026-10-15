"""Diagnostics: what the tool tells the user about each file."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Severity(str, Enum):
    ERROR = "error"  # the converted code will not work until someone fixes it
    WARNING = "warning"  # converted, but the behaviour may differ
    INFO = "info"  # converted, noted for traceability


@dataclass(frozen=True)
class Rule:
    code: str
    severity: Severity
    title: str


_ALL_RULES = [
    Rule("T2M000", Severity.ERROR, "File could not be parsed"),
    Rule("T2M001", Severity.ERROR, "Dynamic workflow, activity or handler is not supported"),
    Rule("T2M002", Severity.ERROR, "Missing parameter type annotation (required by Mistral)"),
    Rule("T2M003", Severity.INFO, "Added `-> None` return annotation (required by Mistral)"),
    Rule("T2M004", Severity.ERROR, "Missing return type annotation (required by Mistral)"),
    Rule("T2M005", Severity.WARNING, "`NoReturn`/`Never` return annotation replaced by `None`"),
    Rule("T2M006", Severity.ERROR, "Type has no JSON schema (plain class needing a custom converter)"),
    Rule("T2M010", Severity.WARNING, "Workflow execution timeout now defaults to 1 hour"),
    Rule("T2M011", Severity.WARNING, "`@workflow.defn` option has no Mistral equivalent and was dropped"),
    Rule("T2M012", Severity.ERROR, "`@workflow.init` is not supported by Mistral"),
    Rule("T2M013", Severity.ERROR, "Update validators are not supported by Mistral"),
    Rule("T2M014", Severity.WARNING, "Handler option has no Mistral equivalent and was dropped"),
    Rule("T2M015", Severity.ERROR, "Query and update handlers must return a value in Mistral"),
    Rule("T2M020", Severity.INFO, "Activity call-site options moved to the `@activity` decorator"),
    Rule("T2M021", Severity.WARNING, "Call sites disagree on activity options; the most permissive value was kept"),
    Rule("T2M022", Severity.WARNING, "Activity option is not a literal and could not be moved"),
    Rule("T2M023", Severity.WARNING, "Activity call option has no Mistral equivalent and was dropped"),
    Rule("T2M024", Severity.WARNING, "RetryPolicy field has no Mistral equivalent and was dropped"),
    Rule("T2M025", Severity.INFO, "No call site found for activity; Mistral defaults apply"),
    Rule("T2M026", Severity.INFO, "Unlimited retries kept (Temporal default) instead of Mistral's 3 attempts"),
    Rule("T2M030", Severity.ERROR, "Class-based (method) activities are not supported by Mistral"),
    Rule("T2M031", Severity.WARNING, "Synchronous activity made asynchronous"),
    Rule("T2M032", Severity.ERROR, "Activity call target could not be resolved to an activity definition"),
    Rule("T2M033", Severity.WARNING, "`start_activity` converted to `asyncio.create_task`"),
    Rule("T2M034", Severity.INFO, "Local activity converted to a regular activity"),
    Rule("T2M035", Severity.WARNING, "Activity chosen at runtime: call converted, options not moved"),
    Rule("T2M036", Severity.ERROR, "Activities defined inside a function are not supported by Mistral"),
    Rule("T2M040", Severity.INFO, "Child workflow call converted to `workflow.execute_workflow`"),
    Rule("T2M041", Severity.ERROR, "Child workflow target could not be resolved"),
    Rule("T2M042", Severity.WARNING, "Child workflow option has no Mistral equivalent and was dropped"),
    Rule("T2M043", Severity.WARNING, "Optional argument omitted with a non-literal default; check the params model"),
    Rule("T2M050", Severity.ERROR, "`continue_as_new` option is not supported by Mistral"),
    Rule("T2M060", Severity.INFO, "Temporal API kept through a direct `temporalio` import"),
    Rule("T2M061", Severity.WARNING, "Temporal API kept, but Mistral offers a dedicated equivalent"),
    Rule("T2M062", Severity.ERROR, "Temporal API has no Mistral support (Nexus, async completion, ...)"),
    Rule("T2M070", Severity.WARNING, "Temporal `Worker` must be replaced by `run_worker`"),
    Rule("T2M071", Severity.WARNING, "Temporal client calls must be replaced by the Mistral API client"),
    Rule("T2M090", Severity.ERROR, "The Mistral SDK rejects the converted module at import time"),
    Rule("T2M091", Severity.INFO, "Module not import-checked: the original does not import in this environment"),
]

# Every rule must also appear in README.md (a test checks it).
RULES: dict[str, Rule] = {rule.code: rule for rule in _ALL_RULES}

SEVERITY_ORDER = [Severity.ERROR, Severity.WARNING, Severity.INFO]


@dataclass
class Diagnostic:
    code: str
    path: str
    line: int
    message: str
    hint: str | None = None

    @property
    def severity(self) -> Severity:
        return RULES[self.code].severity

    @property
    def title(self) -> str:
        return RULES[self.code].title


class Diagnostics:
    """The list of diagnostics of a migration, without duplicates."""

    def __init__(self) -> None:
        self.items: list[Diagnostic] = []
        self._seen: set[tuple] = set()

    def add(
        self,
        code: str,
        path: str,
        line: int,
        message: str,
        hint: str | None = None,
        once_per_file: bool = False,
    ) -> None:
        """Record a diagnostic.

        With ``once_per_file``, the same message is reported only for its first line in a
        file (useful for an API used many times, such as ``workflow.logger``).
        """
        if code not in RULES:
            raise ValueError(f"Unknown diagnostic code: {code}")
        key = (code, path, message) if once_per_file else (code, path, message, line)
        if key in self._seen:
            return
        self._seen.add(key)
        self.items.append(Diagnostic(code, path, line, message, hint))

    def extend(self, other: Diagnostics) -> None:
        for d in other.items:
            self.add(d.code, d.path, d.line, d.message, d.hint)

    def sorted(self) -> list[Diagnostic]:
        return sorted(self.items, key=lambda d: (d.path, d.line, SEVERITY_ORDER.index(d.severity), d.code))

    def count(self, severity: Severity) -> int:
        return len([d for d in self.items if d.severity == severity])

"""Converted code must be accepted, and run, by the real Mistral Workflows SDK.

Skipped unless `mistralai-workflows` is installed (`pip install -e .[test,mistral]`).
Workflows run in the SDK's local mode (no Temporal server): the entrypoint is
called directly and activities run in-process with their retry policy.
`workflow.logger` is left out of the sample: it is kept from Temporal and needs a
real workflow event loop (it works under a worker, not in local mode).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("mistralai.workflows")
pytestmark = pytest.mark.sdk

from temporal2mistral.context import Options  # noqa: E402
from temporal2mistral.project import migrate  # noqa: E402

GREETING = """
from dataclasses import dataclass
from datetime import timedelta
from typing import NoReturn

from temporalio import activity, workflow
from temporalio.common import RetryPolicy


@dataclass
class ComposeGreetingInput:
    greeting: str
    name: str


@activity.defn
def compose_greeting(input: ComposeGreetingInput) -> str:
    activity.logger.info("composing")
    return f"{input.greeting}, {input.name}!"


@activity.defn
async def shout(text: str) -> str:
    return text.upper()


@activity.defn
async def never_returns() -> NoReturn:
    raise RuntimeError("boom")


@workflow.defn
class ComposeGreetingWorkflow:
    @workflow.run
    async def run(self, input: ComposeGreetingInput) -> str:
        return await workflow.execute_activity(
            compose_greeting, input, start_to_close_timeout=timedelta(seconds=10)
        )


@workflow.defn
class GreetingWorkflow:
    def __init__(self) -> None:
        self.greetings = []

    @workflow.run
    async def run(self, name: str, loud: bool = False) -> str:
        text = await workflow.execute_child_workflow(
            ComposeGreetingWorkflow.run, ComposeGreetingInput("Hello", name), id="child"
        )
        if loud:
            text = await workflow.execute_activity(
                shout,
                text,
                start_to_close_timeout=timedelta(seconds=5),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        self.greetings.append(text)
        return text

    @workflow.signal
    def add(self, greeting: str):
        self.greetings.append(greeting)

    @workflow.query
    def count(self) -> int:
        return len(self.greetings)

    @workflow.update
    async def clear(self) -> int:
        n = len(self.greetings)
        self.greetings.clear()
        return n
"""

RUNNER = r"""
import asyncio, json, sys
sys.path.insert(0, sys.argv[1])
import pydantic
import mistralai.workflows as workflows
from mistralai.workflows import get_workflow_definition
import greeting as g

spec = get_workflow_definition(g.GreetingWorkflow)
Params = pydantic.create_model("Params", name=(str, ...), loud=(bool, False))

async def main():
    quiet = await workflows.execute_workflow(g.GreetingWorkflow, params=Params(name="Louis"))
    loud = await workflows.execute_workflow(g.GreetingWorkflow, params=Params(name="Louis", loud=True))
    return {
        "quiet": quiet,
        "loud": loud,
        "name": spec.name,
        "signals": [s.name for s in spec.signals],
        "queries": [q.name for q in spec.queries],
        "updates": [u.name for u in spec.updates],
        "input_schema": sorted(spec.input_schema["properties"]),
    }

print("RESULT " + json.dumps(asyncio.run(main())))
"""


def test_converted_greeting_runs_on_the_mistral_sdk(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "greeting.py").write_text(textwrap.dedent(GREETING).lstrip())
    result = migrate([src], Options(execution_timeout=3600))
    (converted,) = [f for f in result.files if f.path.name == "greeting.py"]
    out = tmp_path / "out"
    out.mkdir()
    (out / "greeting.py").write_text(converted.converted)

    env = {**os.environ, "LOG_LEVEL": "ERROR", "OTEL_ENABLED": "false"}
    proc = subprocess.run(
        [sys.executable, "-c", RUNNER, str(out)], capture_output=True, text=True, env=env, cwd=out, timeout=120
    )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
    assert proc.returncode == 0 and lines, proc.stdout[-2000:] + proc.stderr[-4000:]
    data = json.loads(lines[-1][len("RESULT ") :])
    assert data["quiet"] == "Hello, Louis!"
    assert data["loud"] == "HELLO, LOUIS!"
    assert data["name"] == "GreetingWorkflow"
    assert data["signals"] == ["add"] and data["queries"] == ["count"] and data["updates"] == ["clear"]
    assert data["input_schema"] == ["loud", "name"]


SAMPLES = Path(os.environ.get("T2M_SAMPLES", "")) if os.environ.get("T2M_SAMPLES") else None


@pytest.mark.skipif(SAMPLES is None, reason="set T2M_SAMPLES to a checkout of temporalio/samples-python")
def test_samples_hello_import_with_the_sdk(tmp_path: Path) -> None:
    """Every converted `hello/` sample without a reported error is accepted by the SDK."""
    from temporal2mistral.import_check import check_imports

    result = migrate([SAMPLES / "hello"], Options())
    result.root = SAMPLES  # import as `hello.<module>`
    import_diags = check_imports(result, SAMPLES)
    rejected = [d for d in import_diags.items if d.code == "T2M090"]
    flagged = {d.path for d in result.diagnostics.items if d.severity.value == "error"}
    unexplained = [d for d in rejected if d.path not in flagged]
    assert not unexplained, [(d.path, d.message) for d in unexplained]

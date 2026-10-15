"""End-to-end: run converted workflows on a real Temporal test server through the Mistral SDK.

Needs `mistralai-workflows` and network access to temporal.download (the time-skipping
test server is downloaded on first use). Skipped otherwise; it runs in CI.
"""

from __future__ import annotations

import importlib
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

pytest.importorskip("mistralai.workflows")
pytestmark = pytest.mark.e2e

from temporal2mistral.context import Options  # noqa: E402
from temporal2mistral.project import migrate  # noqa: E402

SOURCE = """
from dataclasses import dataclass
from datetime import timedelta

from temporalio import activity, workflow


@dataclass
class ComposeGreetingInput:
    greeting: str
    name: str


@activity.defn
def compose_greeting(input: ComposeGreetingInput) -> str:
    return f"{input.greeting}, {input.name}!"


@workflow.defn
class E2EChildWorkflow:
    @workflow.run
    async def run(self, input: ComposeGreetingInput) -> str:
        return await workflow.execute_activity(compose_greeting, input, start_to_close_timeout=timedelta(seconds=10))


@workflow.defn
class E2EGreetingWorkflow:
    def __init__(self) -> None:
        self.approved = False

    @workflow.run
    async def run(self, name: str) -> str:
        workflow.logger.info("waiting for approval")
        await workflow.wait_condition(lambda: self.approved)
        return await workflow.execute_child_workflow(E2EChildWorkflow.run, ComposeGreetingInput("Hello", name))

    @workflow.signal
    def approve(self) -> None:
        self.approved = True
"""


@pytest.fixture
def converted_module(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "e2e_greeting.py").write_text(textwrap.dedent(SOURCE).lstrip())
    result = migrate([src], Options(execution_timeout=600))
    out = tmp_path / "out"
    out.mkdir()
    for f in result.files:
        (out / f.path.name).write_text(f.converted)
    sys.path.insert(0, str(out))
    try:
        yield importlib.import_module("e2e_greeting")
    finally:
        sys.path.remove(str(out))
        sys.modules.pop("e2e_greeting", None)


async def test_signal_child_and_activity_on_temporal(converted_module) -> None:
    from mistralai.workflows import config
    from mistralai.workflows.core.activity import get_all_temporal_activities
    from mistralai.workflows.testing.test_worker import create_test_worker
    from temporalio.testing import WorkflowEnvironment

    try:
        env = await WorkflowEnvironment.start_time_skipping()
    except Exception as exc:  # no network access to temporal.download
        pytest.skip(f"Temporal test server unavailable: {exc}")

    config.worker.deployment_name = "test-task-queue"
    m = converted_module
    async with env:
        async with create_test_worker(
            env,
            workflows=[m.E2EGreetingWorkflow, m.E2EChildWorkflow],
            activities=get_all_temporal_activities(),
        ):
            handle = await env.client.start_workflow(
                "E2EGreetingWorkflow",
                {"name": "Louis"},
                id=f"e2e-{uuid.uuid4()}",
                task_queue="test-task-queue",
            )
            await handle.signal("approve", {})
            result = await handle.result()
    value = result["result"] if isinstance(result, dict) and "result" in result else result
    assert value == "Hello, Louis!"

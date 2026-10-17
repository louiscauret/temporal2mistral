"""End-to-end: run converted workflows on a real Temporal test server through the Mistral SDK.

Needs `mistralai-workflows` and network access to temporal.download (the time-skipping
test server is downloaded on first use). Skipped otherwise; it runs in CI.
"""

from __future__ import annotations

import asyncio
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


# A class-based activity: converted to a module-level activity that gets the instance with Depends
class Punctuation:
    instances = 0

    def __init__(self) -> None:
        Punctuation.instances += 1
        self.mark = "!"

    @activity.defn
    async def emphasize(self, text: str) -> str:
        return text + self.mark


@workflow.defn
class E2EChildWorkflow:
    @workflow.run
    async def run(self, input: ComposeGreetingInput) -> str:
        text = await workflow.execute_activity(compose_greeting, input, start_to_close_timeout=timedelta(seconds=10))
        for _ in range(2):
            text = await workflow.execute_activity_method(
                Punctuation.emphasize, text, start_to_close_timeout=timedelta(seconds=10)
            )
        return text


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


def _registered_activities_not_added_by_test_worker() -> list:
    """Every activity the Mistral SDK registered (the converted ones and its internal ones),
    minus the event activities `create_test_worker` adds itself: Temporal refuses duplicate names."""
    from mistralai.workflows.core.activity import get_all_temporal_activities
    from mistralai.workflows.testing import test_worker
    from temporalio.activity import _Definition

    added_by_test_worker = {
        _Definition.must_from_callable(getattr(test_worker, name)).name
        for name in dir(test_worker)
        if name.startswith(("_emit_", "_persist_")) and _Definition.from_callable(getattr(test_worker, name))
    }
    return [
        fn
        for fn in get_all_temporal_activities()
        if _Definition.must_from_callable(fn).name not in added_by_test_worker
    ]


async def test_signal_child_and_activity_on_temporal(converted_module, monkeypatch) -> None:
    from mistralai.workflows import config
    from mistralai.workflows.testing.test_worker import create_test_worker
    from temporalio.testing import WorkflowEnvironment

    try:
        env = await WorkflowEnvironment.start_time_skipping()
    except Exception as exc:  # no network access to temporal.download
        pytest.skip(f"Temporal test server unavailable: {exc}")

    config.worker.deployment_name = "test-task-queue"
    # The SDK tags workflows with an `OtelTraceId` search attribute that the test server does not know:
    # every workflow task would be rejected and the workflow would never finish. The SDK's own tests mock it too.
    monkeypatch.setattr(
        "mistralai.workflows.core.tracing.utils.temporalio.workflow.upsert_search_attributes", lambda *args: None
    )
    m = converted_module
    async with env:
        async with create_test_worker(
            env,
            workflows=[m.E2EGreetingWorkflow, m.E2EChildWorkflow],
            activities=_registered_activities_not_added_by_test_worker(),
        ):
            handle = await env.client.start_workflow(
                "E2EGreetingWorkflow",
                {"name": "Louis"},
                id=f"e2e-{uuid.uuid4()}",
                task_queue="test-task-queue",
            )
            await handle.signal("approve", {})
            result = await asyncio.wait_for(handle.result(), timeout=60)  # fail instead of hanging forever
    value = result["result"] if isinstance(result, dict) and "result" in result else result
    assert value == "Hello, Louis!!!"
    assert m.Punctuation.instances == 1  # one instance for the worker, as on Temporal

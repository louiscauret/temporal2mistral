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
import re
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


CLIENT_WORKFLOW = """
from dataclasses import dataclass

from pydantic import BaseModel
from temporalio import workflow


@dataclass
class Greeting:
    text: str


class Order(BaseModel):
    item: str


@workflow.defn(name="greeting")
class GreetingWorkflow:
    @workflow.run
    async def run(self, greeting: Greeting, count: int = 1) -> str:
        return greeting.text * count

    @workflow.signal
    async def approve(self, approver: str) -> None:
        self.approver = approver

    @workflow.query
    def status(self) -> str:
        return "ok"

    @workflow.update(name="add-order")
    async def add(self, order: Order) -> int:
        return 1
"""

CLIENT_STARTER = """
from temporalio.client import Client

from client_workflow import Greeting, GreetingWorkflow, Order


async def main() -> list:
    client = await Client.connect("localhost:7233")
    result = await client.execute_workflow(GreetingWorkflow.run, Greeting("Hi"), id="run-1", task_queue="q")
    handle = await client.start_workflow(GreetingWorkflow.run, args=[Greeting("Yo"), 2], id="run-2", task_queue="q")
    await handle.signal(GreetingWorkflow.approve, "Louis")
    status = await handle.query(GreetingWorkflow.status)
    count = await handle.execute_update(GreetingWorkflow.add, Order(item="tea"))
    final = await handle.result()
    return [result, handle.id, status, count, final]
"""

# Runs a starter against a fake Mistral API that records the requests it receives.
CLIENT_RUNNER = """
import asyncio, json, sys
import httpx
import mistralai.client

sys.path.insert(0, sys.argv[1])
requests = []

def respond(request):
    body = json.loads(request.content) if request.content else None
    requests.append({"method": request.method, "path": request.url.path, "body": body})
    path = request.url.path
    execution = {
        "workflow_name": "greeting", "root_execution_id": "x", "start_time": "2026-01-01T00:00:00Z",
        "end_time": None, "result": None, "status": "RUNNING",
    }
    if path.endswith("/execute"):
        return httpx.Response(200, json={**execution, "execution_id": body.get("execution_id")})
    if path.endswith("/signals"):
        return httpx.Response(202, json={"message": "Signal accepted"})
    if path.endswith("/queries"):
        return httpx.Response(200, json={"query_name": body["name"], "result": "ok"})
    if path.endswith("/updates"):
        return httpx.Response(200, json={"update_name": body["name"], "result": 1})
    return httpx.Response(200, json={**execution, "execution_id": path.split("/")[-1], "status": "COMPLETED", "result": "done"})

real_client = mistralai.client.Mistral
def fake_client(**kwargs):
    return real_client(**kwargs, async_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
mistralai.client.Mistral = fake_client

import mistral_starter
returned = asyncio.run(mistral_starter.main())
print("RESULT " + json.dumps({"returned": returned, "requests": requests}))
"""


def test_client_translations_call_the_mistral_api(tmp_path: Path) -> None:
    """The Mistral code given in the T2M071 hints, assembled into a starter, sends the right requests."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "client_workflow.py").write_text(textwrap.dedent(CLIENT_WORKFLOW).lstrip())
    (src / "client_starter.py").write_text(textwrap.dedent(CLIENT_STARTER).lstrip())
    result = migrate([src], Options(execution_timeout=3600))
    out = tmp_path / "out"
    out.mkdir()
    for file in result.files:
        (out / file.path.name).write_text(file.converted)

    hints = []
    for diagnostic in result.diagnostics.sorted():
        if diagnostic.code == "T2M071" and diagnostic.message.startswith("`"):
            hints.append(re.findall(r"`([^`]+)`", diagnostic.hint))
    execute, start, signal, query, update = hints
    (out / "mistral_starter.py").write_text(
        "from mistralai.client import Mistral\n"
        "from client_workflow import Greeting, Order\n\n\n"
        "async def main() -> list:\n"
        '    client = Mistral(api_key="test-key")\n'
        f"    result = {execute[0]}\n"
        f"    {start[0]}\n"
        f"    {signal[0]}\n"
        f"    status = {query[0]}\n"
        f"    count = {update[0]}\n"
        f"    final = {start[2]}\n"
        "    return [result, handle.execution_id, status, count, final]\n"
    )

    env = {**os.environ, "LOG_LEVEL": "ERROR", "OTEL_ENABLED": "false"}
    proc = subprocess.run(
        [sys.executable, "-c", CLIENT_RUNNER, str(out)], capture_output=True, text=True, env=env, cwd=out, timeout=120
    )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
    assert proc.returncode == 0 and lines, proc.stdout[-2000:] + proc.stderr[-4000:]
    data = json.loads(lines[-1][len("RESULT ") :])
    assert data["returned"] == ["done", "run-2", "ok", 1, "done"]
    # The client adds fields of its own (tracing...): compare the ones the hints set
    calls = []
    for request in data["requests"]:
        body = request["body"]
        if body is not None:
            body = {key: value for key, value in body.items() if key in ("execution_id", "input", "name")}
        calls.append((request["method"], request["path"], body))
    assert calls == [
        ("POST", "/v1/workflows/greeting/execute", {"execution_id": "run-1", "input": {"greeting": {"text": "Hi"}}}),
        ("GET", "/v1/workflows/executions/run-1", None),
        (
            "POST",
            "/v1/workflows/greeting/execute",
            {"execution_id": "run-2", "input": {"greeting": {"text": "Yo"}, "count": 2}},
        ),
        ("POST", "/v1/workflows/executions/run-2/signals", {"name": "approve", "input": {"approver": "Louis"}}),
        ("POST", "/v1/workflows/executions/run-2/queries", {"name": "status"}),
        ("POST", "/v1/workflows/executions/run-2/updates", {"name": "add-order", "input": {"item": "tea"}}),
        ("GET", "/v1/workflows/executions/run-2", None),
    ]

"""Behaviour of the codemod, one Temporal feature at a time."""

from __future__ import annotations

import ast
import re

from temporal2mistral.context import Options
from temporal2mistral.project import migrate

HELLO = """
from dataclasses import dataclass
from datetime import timedelta

from temporalio import activity, workflow


@dataclass
class ComposeGreetingInput:
    greeting: str
    name: str


@activity.defn
def compose_greeting(input: ComposeGreetingInput) -> str:
    activity.logger.info("Running activity with parameter %s" % input)
    return f"{input.greeting}, {input.name}!"


@workflow.defn
class GreetingWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        workflow.logger.info("Running workflow with parameter %s" % name)
        return await workflow.execute_activity(
            compose_greeting,
            ComposeGreetingInput("Hello", name),
            start_to_close_timeout=timedelta(seconds=10),
        )
"""

HELLO_EXPECTED = """
from dataclasses import dataclass
from datetime import timedelta
import asyncio
from temporalio import activity as temporal_activity, workflow as temporal_workflow
from mistralai.workflows import activity, workflow


@dataclass
class ComposeGreetingInput:
    greeting: str
    name: str


def _compose_greeting_sync(input: ComposeGreetingInput) -> str:
    temporal_activity.logger.info("Running activity with parameter %s" % input)
    return f"{input.greeting}, {input.name}!"


@activity(start_to_close_timeout=timedelta(seconds=10), retry_policy_max_attempts=0)
async def compose_greeting(input: ComposeGreetingInput) -> str:
    return await asyncio.to_thread(_compose_greeting_sync, input)


@workflow.define(name="GreetingWorkflow")
class GreetingWorkflow:
    @workflow.entrypoint
    async def run(self, name: str) -> str:
        temporal_workflow.logger.info("Running workflow with parameter %s" % name)
        return await compose_greeting(ComposeGreetingInput("Hello", name))
""".lstrip("\n")


def test_hello_activity_golden(convert):
    out = convert({"hello.py": HELLO})
    assert out.files["hello.py"] == HELLO_EXPECTED
    assert "T2M010" in out.codes()  # no execution timeout on Temporal
    assert "T2M031" in out.codes()  # sync activity wrapped
    assert "T2M026" in out.codes()  # unlimited retries kept


def test_converted_code_is_idempotent(convert, tmp_path):
    out = convert({"hello.py": HELLO})
    again = tmp_path / "again"
    again.mkdir()
    (again / "hello.py").write_text(out.files["hello.py"])
    result = migrate([again], Options())
    assert all(f.converted == f.original for f in result.files)


def test_async_activity_and_cross_module_options(convert):
    out = convert(
        {
            "app/activities.py": """
                from temporalio import activity

                @activity.defn
                async def charge(order_id: str, amount: int) -> str:
                    activity.heartbeat()
                    return order_id
            """,
            "app/workflows.py": """
                from datetime import timedelta
                from temporalio import workflow
                from temporalio.common import RetryPolicy

                with workflow.unsafe.imports_passed_through():
                    from app.activities import charge

                @workflow.defn(name="orders")
                class OrderWorkflow:
                    @workflow.run
                    async def run(self, order_id: str) -> str:
                        return await workflow.execute_activity(
                            charge,
                            args=[order_id, 42],
                            start_to_close_timeout=timedelta(minutes=2),
                            heartbeat_timeout=timedelta(seconds=30),
                            retry_policy=RetryPolicy(maximum_attempts=5, backoff_coefficient=3.0),
                        )
            """,
        }
    )
    acts, wfs = out.files["app/activities.py"], out.files["app/workflows.py"]
    assert (
        "@activity(start_to_close_timeout=timedelta(minutes=2), heartbeat_timeout=timedelta(seconds=30), "
        "retry_policy_max_attempts=5, retry_policy_backoff_coefficient=3)" in acts
    )
    assert "from datetime import timedelta" in acts
    assert "activity_heartbeat()" in acts
    assert "async def charge(order_id: str, amount: int) -> str:" in acts
    assert "return await charge(order_id, 42)" in wfs
    assert '@workflow.define(name="orders")' in wfs
    assert "with workflow.unsafe.imports_passed_through():" in wfs
    assert "RetryPolicy" not in wfs  # unused import removed
    assert "temporalio" not in wfs


def test_retry_and_timeout_constants_are_resolved(convert):
    out = convert(
        {
            "flow.py": """
                from datetime import timedelta
                from temporalio import activity, workflow
                from temporalio.common import RetryPolicy

                RETRY = RetryPolicy(maximum_attempts=2, initial_interval=timedelta(seconds=1))
                TIMEOUT = timedelta(hours=1)

                @activity.defn
                async def act() -> None: ...

                @workflow.defn
                class W:
                    @workflow.run
                    async def run(self) -> None:
                        await workflow.execute_activity(act, start_to_close_timeout=TIMEOUT, retry_policy=RETRY)
            """
        }
    )
    assert "@activity(start_to_close_timeout=timedelta(hours=1), retry_policy_max_attempts=2)" in out.files["flow.py"]
    assert out.messages("T2M024") == ["`RetryPolicy(initial_interval=...)` dropped for `act`"]


def test_conflicting_call_sites_keep_most_permissive(convert):
    out = convert(
        {
            "flow.py": """
                from datetime import timedelta
                from temporalio import activity, workflow
                from temporalio.common import RetryPolicy

                @activity.defn
                async def act() -> None: ...

                @workflow.defn
                class W:
                    @workflow.run
                    async def run(self) -> None:
                        await workflow.execute_activity(act, start_to_close_timeout=timedelta(seconds=5),
                                                        retry_policy=RetryPolicy(maximum_attempts=2))
                        await workflow.execute_activity(act, start_to_close_timeout=timedelta(seconds=50),
                                                        retry_policy=RetryPolicy(maximum_attempts=4))
            """
        }
    )
    assert (
        "@activity(start_to_close_timeout=timedelta(seconds=50), retry_policy_max_attempts=4)" in out.files["flow.py"]
    )
    assert out.codes().count("T2M021") == 2


def test_mistral_retry_defaults_option(convert):
    src = {
        "flow.py": """
            from datetime import timedelta
            from temporalio import activity, workflow

            @activity.defn
            async def act() -> None: ...

            @workflow.defn
            class W:
                @workflow.run
                async def run(self) -> None:
                    await workflow.execute_activity(act, start_to_close_timeout=timedelta(seconds=5))
        """
    }
    assert "retry_policy_max_attempts=0" in convert(src).files["flow.py"]
    out = convert(src, keep_unlimited_retries=False)
    assert "retry_policy_max_attempts" not in out.files["flow.py"]
    assert "T2M026" not in out.codes()


def test_execution_timeout_option(convert):
    out = convert(
        {
            "w.py": "from temporalio import workflow\n\n@workflow.defn\nclass W:\n    @workflow.run\n    async def run(self) -> int:\n        return 1\n"
        },
        execution_timeout=30 * 86400,
    )
    assert '@workflow.define(name="W", execution_timeout=timedelta(days=30))' in out.files["w.py"]
    assert "from datetime import timedelta" in out.files["w.py"]
    assert "T2M010" not in out.codes()


def test_message_handlers(convert):
    out = convert(
        {
            "w.py": """
                from temporalio import workflow

                @workflow.defn
                class W:
                    def __init__(self) -> None:
                        self.approved = False

                    @workflow.run
                    async def run(self) -> bool:
                        await workflow.wait_condition(lambda: self.approved)
                        return self.approved

                    @workflow.signal
                    async def approve(self, by: str):
                        self.approved = True

                    @workflow.query(name="status")
                    def status(self) -> bool:
                        return self.approved

                    @workflow.update
                    async def reset(self) -> None:
                        self.approved = False

                    @workflow.update
                    async def rename(self, name: str) -> str:
                        return name

                    @rename.validator
                    def check(self, name: str) -> None:
                        if not name:
                            raise ValueError("empty")
            """
        }
    )
    src = out.files["w.py"]
    assert "@workflow.signal()\n    async def approve(self, by: str) -> None:" in src
    assert '@workflow.query(name="status")' in src
    assert "@workflow.update()\n    async def rename" in src
    assert "await workflow.wait_condition(lambda: self.approved)" in src
    assert out.messages("T2M015") == [
        "Update handler `reset` returns nothing; Mistral requires update handlers to return a value"
    ]
    assert "T2M013" in out.codes()
    assert "T2M003" in out.codes()


def test_child_workflow_with_dataclass_input_gets_params_model(convert):
    out = convert(
        {
            "w.py": """
                from dataclasses import dataclass
                from temporalio import workflow

                @dataclass
                class ComposeGreetingInput:
                    greeting: str
                    name: str

                @workflow.defn
                class ComposeGreetingWorkflow:
                    @workflow.run
                    async def run(self, input: ComposeGreetingInput) -> str:
                        return f"{input.greeting}, {input.name}!"

                @workflow.defn
                class GreetingWorkflow:
                    @workflow.run
                    async def run(self, name: str) -> str:
                        return await workflow.execute_child_workflow(
                            ComposeGreetingWorkflow.run,
                            ComposeGreetingInput("Hello", name),
                            id="child-id",
                            task_queue="other",
                        )
            """
        }
    )
    src = out.files["w.py"]
    assert "class ComposeGreetingWorkflowParams(BaseModel):" in src
    assert "    input: Any" in src
    assert (
        "return await workflow.execute_workflow(ComposeGreetingWorkflow, "
        'params=ComposeGreetingWorkflowParams(input=ComposeGreetingInput("Hello", name)), execution_id="child-id")'
    ) in src
    assert "from pydantic import BaseModel" in src and "from typing import Any" in src
    assert "T2M042" in out.codes()  # task_queue dropped
    ast.parse(src)


def test_child_workflow_with_pydantic_input_is_passed_through(convert):
    out = convert(
        {
            "w.py": """
                from pydantic import BaseModel
                from temporalio import workflow

                class Order(BaseModel):
                    id: str

                @workflow.defn
                class Child:
                    @workflow.run
                    async def run(self, order: Order) -> str:
                        return order.id

                @workflow.defn
                class Parent:
                    @workflow.run
                    async def run(self) -> str:
                        handle = await workflow.start_child_workflow(Child.run, Order(id="1"))
                        return await handle
            """
        }
    )
    assert 'await workflow.execute_workflow(Child, params=Order(id="1"), wait=False)' in out.files["w.py"]
    assert "ChildParams" not in out.files["w.py"]


def test_continue_as_new_with_primitive(convert):
    out = convert(
        {
            "w.py": """
                import asyncio
                from temporalio import workflow

                @workflow.defn
                class LoopingWorkflow:
                    @workflow.run
                    async def run(self, iteration: int, label: str = "x") -> None:
                        if iteration == 10:
                            return
                        await asyncio.sleep(1)
                        workflow.continue_as_new(iteration + 1)
            """
        }
    )
    src = out.files["w.py"]
    assert "workflow.continue_as_new(LoopingWorkflowParams(iteration=iteration + 1))" in src
    assert '    label: Any = "x"' in src


def test_unsupported_features_are_reported(convert):
    out = convert(
        {
            "w.py": """
                from datetime import timedelta
                from typing import NoReturn
                from temporalio import activity, workflow

                class Client:
                    pass

                class MyActivities:
                    def __init__(self, db) -> None:
                        self.db = db

                    @activity.defn
                    async def do(self) -> None: ...

                @activity.defn
                async def plain(c: Client) -> NoReturn:
                    raise RuntimeError()

                @workflow.defn
                class W:
                    @workflow.init
                    def __init__(self, name: str) -> None:
                        self.name = name

                    @workflow.run
                    async def run(self, name: str) -> None:
                        await workflow.execute_activity_method(MyActivities.do, start_to_close_timeout=timedelta(seconds=1))
                        await workflow.execute_activity(plain, Client(), start_to_close_timeout=timedelta(seconds=1))
                        workflow.upsert_search_attributes({})
            """
        }
    )
    codes = out.codes()
    for code in ("T2M012", "T2M030", "T2M006", "T2M005", "T2M061"):
        assert code in codes, code
    src = out.files["w.py"]
    assert "@temporal_activity.defn" in src  # left on the class-based activity
    assert "await temporal_workflow.execute_activity_method(" in src
    assert "async def plain(c: Client) -> None:" in src


def test_start_activity_string_name_and_runtime_choice(convert):
    out = convert(
        {
            "w.py": """
                from datetime import timedelta
                from temporalio import activity, workflow

                @activity.defn(name="say-hello")
                async def say_hello(name: str) -> str:
                    return name

                @activity.defn
                async def say_bye(name: str) -> str:
                    return name

                @workflow.defn
                class W:
                    @workflow.run
                    async def run(self, polite: bool) -> str:
                        handle = workflow.start_activity(say_hello, "a", start_to_close_timeout=timedelta(seconds=3))
                        await workflow.execute_activity("say-hello", "b", schedule_to_close_timeout=timedelta(seconds=9))
                        fn = say_hello if polite else say_bye
                        await workflow.execute_activity(fn, "c", start_to_close_timeout=timedelta(seconds=3))
                        return await handle
            """
        }
    )
    src = out.files["w.py"]
    assert 'handle = asyncio.create_task(say_hello("a"))' in src
    assert 'await say_hello("b")' in src
    assert 'await fn("c")' in src
    assert '@activity(name="say-hello", start_to_close_timeout=timedelta(seconds=3)' in src
    assert {"T2M033", "T2M035", "T2M023"} <= set(out.codes())


def test_worker_and_client_code_is_reported_not_rewritten(convert):
    out = convert(
        {
            "starter.py": """
                from temporalio.client import Client
                from temporalio.worker import Worker
                from w import W

                async def main():
                    client = await Client.connect("localhost:7233")
                    async with Worker(client, task_queue="q", workflows=[W]):
                        await client.execute_workflow(W.run, id="x", task_queue="q")
            """,
            "w.py": "from temporalio import workflow\n\n@workflow.defn\nclass W:\n    @workflow.run\n    async def run(self) -> None:\n        pass\n",
        }
    )
    assert out.files["starter.py"].startswith("from temporalio.client import Client")
    hints = [d.hint for d in out.diagnostics if d.code == "T2M070"]
    assert hints == ["`await run_worker([W])` (from mistralai.workflows import run_worker)."]
    assert "T2M071" in out.codes()


def test_input_types_defined_in_files_without_temporal(convert):
    out = convert(
        {
            "models.py": """
                from pydantic import BaseModel

                class ChildInput(BaseModel):
                    name: str

                class Opaque:
                    def __init__(self, value):
                        self.value = value
            """,
            "w.py": """
                from temporalio import workflow
                from models import ChildInput, Opaque

                @workflow.defn
                class Child:
                    @workflow.run
                    async def run(self, input: ChildInput) -> str:
                        return input.name

                @workflow.defn
                class Parent:
                    @workflow.run
                    async def run(self, opaque: Opaque) -> str:
                        return await workflow.execute_child_workflow(Child.run, ChildInput(name="x"))
            """,
        }
    )
    src = out.files["w.py"]
    assert 'workflow.execute_workflow(Child, params=ChildInput(name="x"))' in src
    assert "ChildParams" not in src
    assert out.messages("T2M006") == [
        "`Opaque` (parameter `opaque` of `run`) is a plain class: Mistral cannot build a JSON schema for it"
    ]


def test_module_used_as_a_value_is_kept_from_temporal(convert):
    out = convert(
        {
            "w.py": """
                from temporalio import workflow

                @workflow.defn
                class W:
                    @workflow.run
                    async def run(self, workflow_id: str) -> str:
                        api = workflow
                        return describe(api, workflow=workflow_id)
            """
        }
    )
    src = out.files["w.py"]
    assert "api = temporal_workflow" in src
    assert "describe(api, workflow=workflow_id)" in src  # a keyword named `workflow` is not the module
    assert "from temporalio import workflow as temporal_workflow" in src
    assert "T2M060" in out.codes()


def test_activity_defined_inside_a_function_is_reported(convert):
    out = convert(
        {
            "w.py": """
                from datetime import timedelta
                from temporalio import activity, workflow

                def build(queue: str):
                    @activity.defn
                    async def select_queue() -> str:
                        return queue

                    return select_queue

                @workflow.defn
                class W:
                    @workflow.run
                    async def run(self) -> str:
                        return await workflow.execute_activity(select_queue, start_to_close_timeout=timedelta(seconds=5))
            """
        }
    )
    src = out.files["w.py"]
    assert "    @temporal_activity.defn\n    async def select_queue() -> str:" in src
    assert "await temporal_workflow.execute_activity(select_queue" in src
    assert out.codes().count("T2M036") == 2  # the definition and the call


def test_invalid_duration_is_a_usage_error(tmp_path, capsys):
    import pytest

    from temporal2mistral.cli import main

    with pytest.raises(SystemExit) as exit_info:
        main(["check", str(tmp_path), "--execution-timeout", "soon"])
    assert exit_info.value.code == 2
    assert "invalid duration 'soon'" in capsys.readouterr().err


WORKFLOW_FILE = """
from temporalio import workflow

@workflow.defn
class W:
    @workflow.run
    async def run(self, name: str) -> str:
        return name

    @workflow.signal
    async def approve(self, approver: str) -> None:
        self.approver = approver

    @workflow.query
    def status(self) -> str:
        return "ok"
"""


def test_worker_entry_points_become_run_worker(convert):
    out = convert(
        {
            "w.py": WORKFLOW_FILE,
            "worker.py": """
                import asyncio
                from temporalio.client import Client
                from temporalio.worker import Worker
                from w import W

                async def main(interrupt_event: asyncio.Event) -> None:
                    client = await Client.connect("localhost:7233")
                    async with Worker(client, task_queue="q", workflows=[W]):
                        # Wait until interrupted
                        print("Worker started, ctrl+c to exit")
                        await interrupt_event.wait()
                        print("Shutting down")

                async def other() -> None:
                    client = await Client.connect("localhost:7233")
                    worker = Worker(client, task_queue="q", workflows=[W], activities=[])
                    print("starting")
                    await worker.run()
            """,
        }
    )
    src = out.files["worker.py"]
    assert "Client.connect" not in src
    assert "Worker(" not in src
    assert src.count("await run_worker([W])") == 2
    assert '    # Wait until interrupted\n    print("Worker started, ctrl+c to exit")\n    await run_worker([W])' in src
    assert '    print("starting")\n    await run_worker([W])' in src
    assert "from mistralai.workflows import run_worker" in src
    assert out.codes().count("T2M072") == 2
    assert "T2M070" not in out.codes() and "T2M071" not in out.codes()


def test_workers_that_need_a_decision_are_reported(convert):
    out = convert(
        {
            "w.py": WORKFLOW_FILE,
            "worker.py": """
                from temporalio.client import Client
                from temporalio.worker import Worker
                from w import W

                async def with_plugins(plugin) -> None:
                    client = await Client.connect("localhost:7233", plugins=[plugin])
                    await Worker(client, task_queue="q", workflows=[W]).run()

                async def with_interceptors(client: Client, interceptor) -> None:
                    await Worker(client, task_queue="q", workflows=[W], interceptors=[interceptor]).run()

                async def starter(client: Client) -> None:
                    async with Worker(client, task_queue="q", workflows=[W]):
                        await client.execute_workflow(W.run, "x", id="x", task_queue="q")
            """,
        }
    )
    assert out.files["worker.py"].count("Worker(") == 3
    assert out.codes().count("T2M070") == 3
    assert "T2M072" not in out.codes()


def test_client_with_a_use_that_cannot_be_converted_is_left_as_is(convert):
    out = convert(
        {
            "w.py": WORKFLOW_FILE,
            "starter.py": """
                from temporalio.client import Client
                from w import W

                async def main() -> None:
                    client = await Client.connect("localhost:7233")
                    handle = await client.start_workflow(W.run, "x", id="x", task_queue="q")
                    await handle.signal(W.approve, "Louis")
                    print(await handle.describe())
            """,
        }
    )
    assert "await Client.connect(" in out.files["starter.py"]
    assert "await handle.signal(W.approve" in out.files["starter.py"]
    assert "T2M071" in out.codes() and "T2M073" not in out.codes()


def test_class_based_activity_built_without_arguments_becomes_a_function(convert):
    out = convert(
        {
            "app/__init__.py": "",
            "app/activities.py": """
                from temporalio import activity

                class Bedrock:
                    def __init__(self) -> None:
                        self.client = object()

                    @activity.defn
                    async def prompt(self, text: str) -> str:
                        return text
            """,
            "app/workflows.py": """
                from datetime import timedelta
                from temporalio import workflow
                from .activities import Bedrock

                @workflow.defn
                class W:
                    @workflow.run
                    async def run(self, text: str) -> str:
                        return await workflow.execute_activity_method(
                            Bedrock.prompt, text, start_to_close_timeout=timedelta(seconds=20)
                        )
            """,
        }
    )
    activities = out.files["app/activities.py"]
    assert "    async def prompt(self, text: str) -> str:" in activities  # the method stays, undecorated
    assert (
        "@activity(start_to_close_timeout=timedelta(seconds=20), retry_policy_max_attempts=0)\n"
        "async def prompt(text: str, instance: Bedrock = Depends(Bedrock)) -> str:\n"
        "    return await instance.prompt(text)\n"
    ) in activities
    workflows = out.files["app/workflows.py"]
    assert "from .activities import prompt" in workflows
    assert "return await prompt(text)" in workflows
    assert "T2M037" in out.codes() and "T2M030" not in out.codes()


def test_every_converted_file_parses(convert):
    out = convert({"hello.py": HELLO})
    for src in out.files.values():
        ast.parse(src)


def test_readme_lists_every_rule():
    from pathlib import Path

    from temporal2mistral.diagnostics import RULES

    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"\|\s*(T2M\d{3})\s*\|", readme))
    assert documented == set(RULES), sorted(set(RULES) ^ documented)

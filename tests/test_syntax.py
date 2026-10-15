import libcst as cst
import pytest

from temporal2mistral.durations import parse_cli_duration, parse_timedelta, timedelta_code
from temporal2mistral.syntax import full_name, import_table

SOURCE = """
import temporalio.workflow
import temporalio.activity as act
from temporalio import workflow
from temporalio.common import RetryPolicy as Retry
from .activities import charge
from . import helpers

with workflow.unsafe.imports_passed_through():
    from app.models import Order
"""


def test_import_table():
    table = import_table(cst.parse_module(SOURCE))
    assert table == {
        "temporalio": "temporalio",
        "act": "temporalio.activity",
        "workflow": "temporalio.workflow",
        "Retry": "temporalio.common.RetryPolicy",
        "charge": ".activities.charge",
        "helpers": ".helpers",
        "Order": "app.models.Order",
    }


@pytest.mark.parametrize(
    "code, expected",
    [
        ("workflow.defn", "temporalio.workflow.defn"),
        ("temporalio.workflow.run", "temporalio.workflow.run"),
        ("act.heartbeat", "temporalio.activity.heartbeat"),
        ("charge", ".activities.charge"),
        ("local_function", "local_function"),
        ("make()()", None),
    ],
)
def test_full_name(code, expected):
    table = import_table(cst.parse_module(SOURCE))
    assert full_name(cst.parse_expression(code), table) == expected


@pytest.mark.parametrize(
    "code, seconds",
    [
        ("timedelta(seconds=10)", 10),
        ("timedelta(minutes=2, seconds=5)", 125),
        ("datetime.timedelta(1)", 86400),
        ("timedelta(seconds=TIMEOUT)", None),
        ("TIMEOUT", None),
    ],
)
def test_parse_timedelta(code, seconds):
    assert parse_timedelta(code) == seconds


def test_timedelta_code_uses_the_largest_exact_unit():
    assert timedelta_code(86400 * 30) == "timedelta(days=30)"
    assert timedelta_code(5400) == "timedelta(minutes=90)"
    assert timedelta_code(61) == "timedelta(seconds=61)"


def test_parse_cli_duration():
    assert parse_cli_duration("30d") == 30 * 86400
    assert parse_cli_duration("12h") == 12 * 3600
    assert parse_cli_duration("90") == 90

from temporal2mistral.activity_options import decorator_options
from temporal2mistral.scan import ActivityCall


def call(options=None, retry_policy=None, retry_policy_code=None, line=1):
    return ActivityCall("workflows.py", line, options or {}, retry_policy, retry_policy_code)


def codes(result):
    return [note.code for note in result.notes]


def test_no_call_keeps_mistral_defaults():
    result = decorator_options("charge", [], keep_unlimited_retries=True)
    assert result.arguments == []
    assert codes(result) == ["T2M025"]


def test_timeout_and_unlimited_retries():
    result = decorator_options("charge", [call({"start_to_close_timeout": "timedelta(seconds=90)"})], True)
    assert result.arguments == ["start_to_close_timeout=timedelta(seconds=90)", "retry_policy_max_attempts=0"]
    assert result.uses_timedelta


def test_mistral_retry_defaults_drop_unlimited_retries():
    result = decorator_options("charge", [call({"start_to_close_timeout": "timedelta(minutes=1)"})], False)
    assert result.arguments == ["start_to_close_timeout=timedelta(minutes=1)"]


def test_retry_policy_fields():
    retry = {"maximum_attempts": "5", "backoff_coefficient": "1.5", "initial_interval": "timedelta(seconds=1)"}
    result = decorator_options("charge", [call(retry_policy=retry)], True)
    assert result.arguments == ["retry_policy_max_attempts=5", "retry_policy_backoff_coefficient=1.5"]
    assert "T2M024" in codes(result)


def test_disagreeing_calls_keep_the_most_permissive_value():
    calls = [
        call({"start_to_close_timeout": "timedelta(seconds=5)"}, {"maximum_attempts": "2"}, line=10),
        call({"start_to_close_timeout": "timedelta(minutes=2)"}, None, line=20),
    ]
    result = decorator_options("charge", calls, True)
    assert result.arguments == ["start_to_close_timeout=timedelta(minutes=2)", "retry_policy_max_attempts=0"]
    disagreements = [note.message for note in result.notes if note.code == "T2M021"]
    assert len(disagreements) == 2
    assert disagreements[1].endswith("kept unlimited")


def test_schedule_to_close_is_used_when_alone():
    result = decorator_options("charge", [call({"schedule_to_close_timeout": "timedelta(hours=1)"})], True)
    assert result.arguments[0] == "start_to_close_timeout=timedelta(hours=1)"
    assert "T2M023" in codes(result)


def test_values_that_are_not_literals_are_reported():
    calls = [call({"start_to_close_timeout": "settings.TIMEOUT"}, retry_policy_code="build_policy()")]
    result = decorator_options("charge", calls, True)
    assert result.arguments == []
    assert codes(result) == ["T2M022", "T2M022"]

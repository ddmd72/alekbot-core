"""DateutilRecurrenceAdapter: the failure paths the mutation check found unpinned (`except` blocks that must not turn a
refusal into a silent success). Each test asserts what a caller gets back: the refusal's reason, the rule text, the
instant computed in UTC."""
import os
import time
from datetime import datetime, timezone

import pytest

from src.adapters.dateutil_recurrence_adapter import DateutilRecurrenceAdapter

UTC = timezone.utc


@pytest.fixture
def adapter():
    return DateutilRecurrenceAdapter()


@pytest.fixture
def host_in_new_york():
    """The process's local zone set to America/New_York for one test, then put back (`time.tzset` both ways)."""
    before = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()


def test_a_rule_dateutil_refuses_is_refused_with_dateutil_s_reason(adapter):
    """dateutil's own refusal reaches the caller (the model reads it to fix the rule); it is not reported as a rule that
    never occurs."""
    with pytest.raises(ValueError) as refused:
        adapter.normalize("FREQ=DAILY;BYFOO=1")
    assert "BYFOO" in str(refused.value)
    assert "never occurs" not in str(refused.value)


def test_a_minutely_interval_that_is_not_a_number_is_refused_as_not_a_number(adapter):
    with pytest.raises(ValueError) as refused:
        adapter.normalize("FREQ=MINUTELY;INTERVAL=abc")
    assert "positive integer" in str(refused.value)
    assert "at least" not in str(refused.value)


def test_describe_returns_the_rule_itself_when_its_minute_is_not_a_number(adapter):
    rule = "FREQ=DAILY;BYHOUR=9;BYMINUTE=xx"
    assert adapter.describe(rule) == rule


def test_an_unknown_timezone_is_evaluated_in_utc_not_in_the_host_zone(adapter, host_in_new_york):
    """09:00 daily from 08:00 UTC in an unknown zone: 09:00 UTC the same day, whatever zone the host runs in."""
    got = adapter.first_occurrence("FREQ=DAILY;BYHOUR=9;BYMINUTE=0", datetime(2026, 10, 27, 8, 0, tzinfo=UTC),
                                   "Mars/Olympus_Mons")
    assert got == datetime(2026, 10, 27, 9, 0, tzinfo=UTC)

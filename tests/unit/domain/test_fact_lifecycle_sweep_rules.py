"""Facts S3 · T9: the janitor's pure rules (plan §3.13, §3.14, §3.17): breaker edges, the stale write fields, the log-only
switch and the ops line."""
from datetime import date, datetime, timedelta, timezone

import pytest

from src.domain.due_fact import DueFact
from src.domain.entities import ExpiryKind
from src.domain.fact_lifecycle_day import FactLifecycleDay
from src.domain.fact_lifecycle_sweep import (
    BREAKER_BACKLOG, BREAKER_REVIEW_AGE, breaker_reason, daily_line, stale_fields, sweep_dry_run,
)

NOW = datetime(2026, 10, 23, 7, 30, tzinfo=timezone.utc)
DAY = date(2026, 10, 23)


def due(events=None):
    return DueFact(fact_id="f1", account_id="acc-1", expires_at=NOW - timedelta(days=3), expiry_kind=ExpiryKind.DATE,
                   update_time=1, lifecycle_events=events or [])


# ------------------------------------------------------------------------------------------------ breaker

def test_the_constants_are_72_hours_and_50_facts():
    assert BREAKER_REVIEW_AGE == timedelta(hours=72) and BREAKER_BACKLOG == 50


def test_no_stale_facts_never_trips_whatever_the_review_age():
    assert breaker_reason(0, None, NOW) is None
    assert breaker_reason(0, NOW - timedelta(days=30), NOW) is None


def test_stale_facts_and_no_review_ever_trips():
    assert breaker_reason(1, None, NOW) is not None


def test_the_review_age_edge_is_exclusive():
    assert breaker_reason(3, NOW - timedelta(hours=72), NOW) is None
    assert breaker_reason(3, NOW - timedelta(hours=72, seconds=1), NOW) is not None
    assert breaker_reason(3, NOW - timedelta(hours=1), NOW) is None


def test_the_backlog_edge_is_exclusive_and_a_fresh_review_does_not_lift_it():
    fresh = NOW - timedelta(hours=1)
    assert breaker_reason(50, fresh, NOW) is None
    reason = breaker_reason(51, fresh, NOW)
    assert reason is not None and "51" in reason and "50" in reason


def test_a_reason_is_text_without_fact_content():
    assert isinstance(breaker_reason(2, None, NOW), str)


# ------------------------------------------------------------------------------------------------ stale_fields

def test_stale_fields_are_exactly_the_stale_write():
    fields = stale_fields(due(), NOW)
    assert set(fields) == {"state", "is_current", "valid_to", "stale_since", "lifecycle_events"} | {"last_updated"}
    assert fields["state"].value == "stale" and fields["is_current"] is True and fields["valid_to"] is None
    assert fields["stale_since"] == NOW and fields["last_updated"] == NOW


def test_stale_fields_append_the_expired_event_to_the_trail():
    earlier = {"at": NOW, "from": None, "to": "current", "actor": "stage1", "reason": "x"}
    fields = stale_fields(due([earlier]), NOW)
    assert fields["lifecycle_events"] == [
        earlier,
        {"at": NOW, "from": "current", "to": "stale", "actor": "janitor",
         "reason": f"expired {(NOW - timedelta(days=3)).isoformat()}"},
    ]


def test_stale_fields_do_not_change_the_input_trail():
    trail = [{"at": NOW, "from": None, "to": "current", "actor": "a", "reason": "r"}]
    stale_fields(due(trail), NOW)
    assert len(trail) == 1


# ------------------------------------------------------------------------------------------------ sweep_dry_run

@pytest.mark.parametrize("raw", [None, "", "  ", "1", "true", "yes", "on", "log-only", "garbage", "00"])
def test_log_only_unless_explicitly_off(raw):
    assert sweep_dry_run(raw) is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "FALSE", " Off ", "No"])
def test_an_explicit_off_value_turns_log_only_off(raw):
    assert sweep_dry_run(raw) is False


# ------------------------------------------------------------------------------------------------ daily_line

def row(account="acc-12345678-xyz", **over):
    values = dict(account_id=account, day=DAY, run_at=NOW, due=4, newly_due=3, no_decision=1, staled=2, would_stale=0,
                  stale_conflicts=1, breaker="ok", backlog=5, oldest_stale_days=9, expiry_tool_errors=2)
    values.update(over)
    return FactLifecycleDay(**values)


def test_the_line_names_the_run_the_mode_and_every_figure():
    text = daily_line(NOW, [row()], dry_run=False)
    assert text == (
        "🧹 Fact lifecycle — run 2026-10-23 07:30 UTC\n"
        "acc-1234: due 4 (new 3, no decision 1) | staled 2 / would stale 0 | conflicts 1 | breaker ok"
        " | backlog 5 (oldest 9d) | expiry tool errors yesterday 2"
    )


def test_log_only_is_in_the_header():
    assert daily_line(NOW, [row()], dry_run=True).splitlines()[0].endswith("UTC · log-only")


def test_the_header_time_is_utc_whatever_the_zone_of_run_at():
    local = NOW.astimezone(timezone(timedelta(hours=2)))
    assert "run 2026-10-23 07:30 UTC" in daily_line(local, [row()], dry_run=False)


def test_an_unknown_oldest_age_is_left_out_and_an_empty_breaker_reads_ok():
    text = daily_line(NOW, [row(oldest_stale_days=None, breaker="")], dry_run=False)
    assert "breaker ok | backlog 5 | expiry" in text and "oldest" not in text


def test_a_tripped_and_a_would_trip_breaker_are_shown_as_stored():
    assert "breaker tripped" in daily_line(NOW, [row(breaker="tripped")], dry_run=False)
    assert "breaker would trip" in daily_line(NOW, [row(breaker="would trip")], dry_run=True)


def test_no_line_when_every_figure_is_zero_and_the_breaker_is_ok():
    quiet = row(due=0, newly_due=0, no_decision=0, staled=0, would_stale=0, stale_conflicts=0, backlog=0,
                oldest_stale_days=None, expiry_tool_errors=0)
    assert daily_line(NOW, [quiet], dry_run=False) is None
    assert daily_line(NOW, [], dry_run=False) is None


def test_each_figure_alone_earns_a_line():
    zero = dict(due=0, newly_due=0, no_decision=0, staled=0, would_stale=0, stale_conflicts=0, backlog=0,
                oldest_stale_days=None, expiry_tool_errors=0)
    for name in ("due", "newly_due", "no_decision", "staled", "would_stale", "stale_conflicts", "backlog",
                 "expiry_tool_errors"):
        assert daily_line(NOW, [row(**{**zero, name: 1})], dry_run=False) is not None, name
    assert daily_line(NOW, [row(**{**zero, "breaker": "would trip"})], dry_run=True) is not None


def test_quiet_accounts_are_left_out_and_the_rest_are_ordered_by_account():
    quiet = row(account="zzz-quiet", due=0, newly_due=0, no_decision=0, staled=0, would_stale=0, stale_conflicts=0,
                backlog=0, oldest_stale_days=None, expiry_tool_errors=0)
    lines = daily_line(NOW, [row(account="bbb-account"), quiet, row(account="aaa-account")], dry_run=False).splitlines()
    assert [line[:3] for line in lines[1:]] == ["aaa", "bbb"]

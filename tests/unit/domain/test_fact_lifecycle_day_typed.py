"""Facts S4 · T4b (S1–S3 inward review L1): the janitor's snapshot is a typed value, the counters an enum, and one
constructor builds the day's row from a snapshot."""
from dataclasses import FrozenInstanceError, fields
from datetime import date, datetime, timezone

import pytest

from src.domain.fact_lifecycle_day import CounterField, FactLifecycleDay, SweepSnapshot, check_count

DAY = date(2026, 10, 24)
RUN_AT = datetime(2026, 10, 24, 7, 0, tzinfo=timezone.utc)
S4_COUNTERS = (
    "reviewed revived rewritten archived_by_review invalidated superseded kept queued questions_sent not_delivered "
    "answered_by_digit answered_by_button answered_in_cabinet answers_not_applied closed_by_reply timed_out "
    "review_failed review_refusals replies_in_words already_answered reply_intake_failed labels_fallback"
).split()
# P2 revised (contract C12): the history rewrites, the folded parts of a plan, the history fallbacks that did not land.
P2_COUNTERS = ["to_history", "folded", "fallbacks_not_applied"]


def snapshot(**over) -> SweepSnapshot:
    values = dict(run_at=RUN_AT, due=4, newly_due=3, no_decision=1, staled=2, would_stale=0, deferred=1,
                  stale_conflicts=1, breaker="ok", backlog=9, oldest_stale_days=11, review_enqueued=5)
    values.update(over)
    return SweepSnapshot(**values)


def test_the_counters_are_the_s3_one_and_the_s4_ones_of_plan_2_2():
    assert [c.value for c in CounterField] == ["expiry_tool_errors", *S4_COUNTERS, *P2_COUNTERS]
    assert all(c == c.value and isinstance(c, str) for c in CounterField)


def test_the_snapshot_holds_the_s3_fields_and_review_enqueued():
    assert [f.name for f in fields(SweepSnapshot)] == [
        "run_at", "due", "newly_due", "no_decision", "staled", "would_stale", "deferred", "stale_conflicts", "breaker",
        "backlog", "oldest_stale_days", "review_enqueued",
    ]


def test_a_day_row_has_every_snapshot_field_and_every_counter_and_nothing_else():
    names = {f.name for f in fields(FactLifecycleDay)}
    snapshot_names = {f.name for f in fields(SweepSnapshot)}
    counter_names = {c.value for c in CounterField}
    assert not snapshot_names & counter_names
    assert names == {"account_id", "day"} | snapshot_names | counter_names


def test_a_snapshot_is_frozen():
    with pytest.raises(FrozenInstanceError):
        snapshot().due = 1  # type: ignore[misc]


@pytest.mark.parametrize("field", ["due", "newly_due", "no_decision", "staled", "would_stale", "deferred",
                                   "stale_conflicts", "backlog", "review_enqueued", "oldest_stale_days"])
@pytest.mark.parametrize("bad", [-1, 1.5, True, "3"])
def test_a_snapshot_number_is_a_count(field, bad):
    with pytest.raises(ValueError, match=field):
        snapshot(**{field: bad})


def test_a_snapshot_needs_a_run_time_and_may_have_no_oldest_age():
    with pytest.raises(ValueError, match="run_at"):
        snapshot(run_at="2026-10-24T07:00:00Z")
    assert snapshot(oldest_stale_days=None).oldest_stale_days is None
    assert snapshot(due=0, oldest_stale_days=0).due == 0


@pytest.mark.parametrize("value", [0, 1, 10**9])
def test_check_count_takes_a_non_negative_int(value):
    check_count("x", value)


@pytest.mark.parametrize("value", [-1, 0.0, 2.5, True, False, None, "1"])
def test_check_count_refuses_anything_else_naming_the_field(value):
    with pytest.raises(ValueError, match="answered_by_digit"):
        check_count("answered_by_digit", value)


def test_of_copies_every_snapshot_field_and_the_given_counters():
    snap = snapshot()
    row = FactLifecycleDay.of("acc-1", DAY, snap, {CounterField.REVIEWED: 2, CounterField.EXPIRY_TOOL_ERRORS: 1})

    assert (row.account_id, row.day) == ("acc-1", DAY)
    assert all(getattr(row, f.name) == getattr(snap, f.name) for f in fields(SweepSnapshot))
    assert (row.reviewed, row.expiry_tool_errors) == (2, 1)
    assert all(getattr(row, c.value) == 0 for c in CounterField if c not in (CounterField.REVIEWED,
                                                                              CounterField.EXPIRY_TOOL_ERRORS))


def test_of_without_a_snapshot_is_a_day_with_no_run():
    row = FactLifecycleDay.of("acc-1", DAY, None, {CounterField.QUEUED: 3})
    assert row == FactLifecycleDay(account_id="acc-1", day=DAY, queued=3)
    assert row.run_at is None and row.breaker == "" and row.oldest_stale_days is None


def test_of_refuses_a_name_that_is_not_a_counter():
    with pytest.raises(ValueError):
        FactLifecycleDay.of("acc-1", DAY, None, {"due": 3})  # type: ignore[dict-item]

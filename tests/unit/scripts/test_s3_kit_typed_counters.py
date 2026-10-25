"""Facts S4 · T4b (S1–S3 inward review L1): the dry-run kit's counters store keeps the typed values it is given and
refuses what the production store refuses, without copies of the store's checks."""
from datetime import date, datetime, timezone

import pytest

from scripts.consolidation.s3_dry_run_kit import RecordingCounters
from src.domain.fact_lifecycle_day import CounterField, FactLifecycleDay, SweepSnapshot

DAY = date(2026, 10, 24)
RUN_AT = datetime(2026, 10, 24, 7, 0, tzinfo=timezone.utc)


def snapshot(**over) -> SweepSnapshot:
    values = dict(run_at=RUN_AT, due=4, newly_due=3, no_decision=1, staled=2, would_stale=0, deferred=1,
                  stale_conflicts=1, breaker="ok", backlog=9, oldest_stale_days=None, review_enqueued=0)
    values.update(over)
    return SweepSnapshot(**values)


async def test_the_kit_records_the_typed_counters_and_snapshots():
    counters = RecordingCounters()
    snap = snapshot()

    await counters.add("acc-1", DAY, {CounterField.REVIEWED: 2, CounterField.KEPT: 0})
    await counters.record_sweep("acc-1", DAY, snap)

    assert counters.adds == [("acc-1", DAY, {CounterField.REVIEWED: 2})]
    assert all(isinstance(name, CounterField) for _, _, counts in counters.adds for name in counts)
    assert counters.sweeps == [("acc-1", DAY, snap)]


async def test_the_kit_reads_a_day_back_as_the_store_would():
    counters = RecordingCounters()
    first, last = snapshot(due=1), snapshot(due=7)
    await counters.record_sweep("acc-2", DAY, first)
    await counters.record_sweep("acc-2", DAY, last)
    await counters.add("acc-1", DAY, {CounterField.EXPIRY_TOOL_ERRORS: 1})
    await counters.add("acc-1", DAY, {CounterField.EXPIRY_TOOL_ERRORS: 2, CounterField.QUEUED: 1})

    assert await counters.for_day(DAY) == [
        FactLifecycleDay.of("acc-1", DAY, None, {CounterField.EXPIRY_TOOL_ERRORS: 3, CounterField.QUEUED: 1}),
        FactLifecycleDay.of("acc-2", DAY, last, {}),
    ]
    assert counters.expiry_tool_errors == 3


@pytest.mark.parametrize("bad", [-1, 1.5, True, None])
async def test_the_kit_refuses_a_count_the_store_refuses_and_records_nothing(bad):
    counters = RecordingCounters()
    with pytest.raises(ValueError):
        await counters.add("acc-1", DAY, {CounterField.REVIEWED: 1, CounterField.QUEUED: bad})
    assert counters.adds == []

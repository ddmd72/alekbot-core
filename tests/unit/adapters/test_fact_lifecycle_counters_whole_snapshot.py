"""Facts S3 · T9 (T8 review F2): `record_sweep` takes a whole snapshot, so "the last run of the day wins" holds for every field
(a partial snapshot under merge would keep the earlier run's value)."""
from datetime import date, datetime, timezone

import pytest

from src.adapters.firestore_fact_lifecycle_counters import FirestoreFactLifecycleCounters
from src.config.environment import EnvironmentConfig
from src.domain.fact_lifecycle_day import SNAPSHOT_FIELDS
from tests.support.firestore_eval import EvalDb

DAY = date(2026, 10, 23)
RUN_AT = datetime(2026, 10, 23, 7, 0, tzinfo=timezone.utc)
WHOLE = {"run_at": RUN_AT, "due": 4, "newly_due": 3, "no_decision": 1, "staled": 0, "would_stale": 3,
         "deferred": 0, "stale_conflicts": 0, "breaker": "would trip", "backlog": 2, "oldest_stale_days": 11}


@pytest.fixture
def db():
    return EvalDb()


@pytest.fixture
def counters(db):
    return FirestoreFactLifecycleCounters(db, EnvironmentConfig())


def test_the_fixture_is_the_whole_field_set():
    assert set(WHOLE) == SNAPSHOT_FIELDS


@pytest.mark.parametrize("field", sorted(SNAPSHOT_FIELDS))
async def test_a_snapshot_missing_any_field_is_refused_and_nothing_is_written(counters, db, field):
    partial = {k: v for k, v in WHOLE.items() if k != field}
    with pytest.raises(ValueError, match=field):
        await counters.record_sweep("acc-1", DAY, partial)
    assert db.collection(EnvironmentConfig().fact_lifecycle_daily_collection).docs == {}


async def test_an_unknown_field_is_refused(counters):
    with pytest.raises(ValueError):
        await counters.record_sweep("acc-1", DAY, {**WHOLE, "expiry_tool_errors": 1})


@pytest.mark.parametrize("bad", [
    {"due": -1}, {"due": 1.5}, {"due": True}, {"due": "4"}, {"backlog": None}, {"oldest_stale_days": -2},
    {"oldest_stale_days": "9"}, {"breaker": None}, {"breaker": 3}, {"run_at": "2026-10-23"}, {"run_at": None},
])
async def test_a_mistyped_value_is_refused(counters, bad):
    with pytest.raises(ValueError):
        await counters.record_sweep("acc-1", DAY, {**WHOLE, **bad})


async def test_oldest_stale_days_none_is_stored_as_null_and_replaces_an_earlier_age(counters, db):
    await counters.record_sweep("acc-1", DAY, WHOLE)
    await counters.record_sweep("acc-1", DAY, {**WHOLE, "oldest_stale_days": None, "backlog": 0})

    doc = db.collection(EnvironmentConfig().fact_lifecycle_daily_collection).docs["acc-1:2026-10-23"]
    assert doc["oldest_stale_days"] is None and doc["backlog"] == 0
    (row,) = await counters.for_day(DAY)
    assert row.oldest_stale_days is None


async def test_a_zero_count_is_a_valid_snapshot_value(counters):
    await counters.record_sweep("acc-1", DAY, {**WHOLE, "due": 0, "oldest_stale_days": 0})
    (row,) = await counters.for_day(DAY)
    assert (row.due, row.oldest_stale_days) == (0, 0)

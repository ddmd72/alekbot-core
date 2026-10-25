"""Facts S4 · T4b (S1–S3 inward review L1): the counters store takes a typed snapshot and typed counters; the stored
document's codec is the adapter's. Asserted on what the evaluating fake holds and what `for_day` reads back."""
import asyncio
from dataclasses import fields
from datetime import date, datetime, timezone

import pytest

from src.adapters.firestore_fact_lifecycle_counters import FirestoreFactLifecycleCounters
from src.config.environment import EnvironmentConfig
from src.domain.fact_lifecycle_day import CounterField, FactLifecycleDay, SweepSnapshot
from tests.support.firestore_eval import EvalDb

DAY = date(2026, 10, 24)
RUN_AT = datetime(2026, 10, 24, 7, 0, tzinfo=timezone.utc)
ACC = "acc-1"


def snapshot(**over) -> SweepSnapshot:
    values = dict(run_at=RUN_AT, due=4, newly_due=3, no_decision=1, staled=2, would_stale=0, deferred=1,
                  stale_conflicts=1, breaker="ok", backlog=9, oldest_stale_days=11, review_enqueued=5)
    values.update(over)
    return SweepSnapshot(**values)


@pytest.fixture
def env():
    return EnvironmentConfig()


@pytest.fixture
def db():
    return EvalDb()


@pytest.fixture
def counters(db, env):
    return FirestoreFactLifecycleCounters(db, env)


def daily(db, env) -> dict:
    return db.collection(env.fact_lifecycle_daily_collection).docs


async def row_of(counters) -> FactLifecycleDay:
    rows = await counters.for_day(DAY)
    assert len(rows) == 1
    return rows[0]


# ------------------------------------------------------------------------------------------------ add

@pytest.mark.parametrize("counter", list(CounterField))
async def test_each_counter_adds_up_under_its_own_name_and_reads_back(counter, counters, db, env):
    await asyncio.gather(*[counters.add(ACC, DAY, {counter: 1}) for _ in range(3)])
    await counters.add(ACC, DAY, {counter: 4})

    doc = daily(db, env)[f"{ACC}:{DAY.isoformat()}"]
    assert doc[counter.value] == 7
    row = await row_of(counters)
    assert getattr(row, counter.value) == 7
    others = [c for c in CounterField if c is not counter]
    assert all(getattr(row, c.value) == 0 for c in others)


async def test_several_counters_in_one_add_each_land_on_their_field(counters):
    await counters.add(ACC, DAY, {CounterField.REVIEWED: 3, CounterField.KEPT: 2, CounterField.TIMED_OUT: 0})

    row = await row_of(counters)
    assert (row.reviewed, row.kept, row.timed_out) == (3, 2, 0)


@pytest.mark.parametrize("bad", [-1, 1.5, True, "2", None])
async def test_a_negative_or_non_integer_count_is_refused_and_nothing_is_written(bad, counters, db, env):
    with pytest.raises(ValueError):
        await counters.add(ACC, DAY, {CounterField.REVIEWED: 1, CounterField.QUEUED: bad})
    assert daily(db, env) == {}


async def test_a_name_that_is_not_a_counter_is_refused(counters, db, env):
    with pytest.raises(ValueError):
        await counters.add(ACC, DAY, {"staled": 1})          # a snapshot field, never added to
    assert daily(db, env) == {}


async def test_only_zero_counts_write_nothing(counters, db, env):
    await counters.add(ACC, DAY, {CounterField.REVIEWED: 0, CounterField.REVIVED: 0})
    assert daily(db, env) == {}


# ------------------------------------------------------------------------------------------------ record_sweep

async def test_the_snapshot_round_trips_through_the_store(counters):
    snap = snapshot()
    await counters.record_sweep(ACC, DAY, snap)

    assert await row_of(counters) == FactLifecycleDay.of(ACC, DAY, snap, {})


async def test_the_stored_snapshot_is_its_whole_field_set_beside_the_account_and_day(counters, db, env):
    await counters.record_sweep(ACC, DAY, snapshot(oldest_stale_days=None))

    doc = daily(db, env)[f"{ACC}:{DAY.isoformat()}"]
    assert set(doc) == {f.name for f in fields(SweepSnapshot)} | {"account_id", "day"}
    assert doc["oldest_stale_days"] is None and doc["review_enqueued"] == 5


async def test_a_later_snapshot_replaces_every_field_and_the_counters_stay(counters):
    await counters.add(ACC, DAY, {CounterField.EXPIRY_TOOL_ERRORS: 2, CounterField.QUESTIONS_SENT: 1})
    await counters.record_sweep(ACC, DAY, snapshot())
    later = snapshot(run_at=RUN_AT.replace(hour=12), due=0, oldest_stale_days=None, review_enqueued=0, breaker="tripped")
    await counters.record_sweep(ACC, DAY, later)

    assert await row_of(counters) == FactLifecycleDay.of(
        ACC, DAY, later, {CounterField.EXPIRY_TOOL_ERRORS: 2, CounterField.QUESTIONS_SENT: 1},
    )


# ------------------------------------------------------------------------------------------------ old documents

async def test_an_old_document_without_the_new_fields_reads_them_as_zero(counters, db, env):
    daily(db, env)[f"{ACC}:{DAY.isoformat()}"] = {          # an S3 document: no review_enqueued, no S4 counter
        "account_id": ACC, "day": DAY.isoformat(), "run_at": RUN_AT, "due": 4, "newly_due": 3, "no_decision": 1,
        "staled": 2, "would_stale": 0, "deferred": 1, "stale_conflicts": 1, "breaker": "ok", "backlog": 9,
        "oldest_stale_days": 11, "expiry_tool_errors": 3,
    }

    row = await row_of(counters)
    assert row == FactLifecycleDay.of(ACC, DAY, snapshot(review_enqueued=0), {CounterField.EXPIRY_TOOL_ERRORS: 3})
    assert row.review_enqueued == 0
    assert all(getattr(row, c.value) == 0 for c in CounterField if c is not CounterField.EXPIRY_TOOL_ERRORS)


async def test_a_document_with_counters_only_has_no_run(counters):
    await counters.add(ACC, DAY, {CounterField.REPLIES_IN_WORDS: 1})

    row = await row_of(counters)
    assert row == FactLifecycleDay(account_id=ACC, day=DAY, replies_in_words=1)
    assert row.run_at is None and row.oldest_stale_days is None

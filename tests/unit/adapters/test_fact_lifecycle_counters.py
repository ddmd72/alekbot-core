"""Facts S3 · T8: FirestoreFactLifecycleCounters on the evaluating fake (FACT_LIFECYCLE_RFC D10, plan §3.16)."""
import asyncio
from datetime import date, datetime, timezone

import pytest

from src.adapters.firestore_fact_lifecycle_counters import FirestoreFactLifecycleCounters
from src.config.environment import EnvironmentConfig
from src.domain.fact_lifecycle_day import COUNTER_FIELDS, SNAPSHOT_FIELDS, FactLifecycleDay
from src.ports.fact_lifecycle_counters_port import FactLifecycleCountersPort
from tests.support.firestore_eval import EvalDb

DAY = date(2026, 10, 23)
NEXT_DAY = date(2026, 10, 24)
RUN_AT = datetime(2026, 10, 23, 7, 0, tzinfo=timezone.utc)


@pytest.fixture
def env():
    return EnvironmentConfig()


@pytest.fixture
def db():
    return EvalDb()


@pytest.fixture
def counters(db, env):
    return FirestoreFactLifecycleCounters(db, env)


def snapshot(**over):
    values = {"run_at": RUN_AT, "due": 4, "newly_due": 3, "no_decision": 1, "staled": 0, "would_stale": 3,
              "stale_conflicts": 0, "breaker": "would trip", "backlog": 2, "oldest_stale_days": 11}
    values.update(over)
    return values


def test_it_is_the_port(counters):
    assert isinstance(counters, FactLifecycleCountersPort)


async def test_record_sweep_writes_one_document_per_account_and_day(counters, db, env):
    await counters.record_sweep("acc-1", DAY, snapshot())

    docs = db.collection(env.fact_lifecycle_daily_collection).docs
    assert list(docs) == ["acc-1:2026-10-23"]
    assert docs["acc-1:2026-10-23"]["day"] == "2026-10-23" and docs["acc-1:2026-10-23"]["account_id"] == "acc-1"
    assert docs["acc-1:2026-10-23"]["due"] == 4 and docs["acc-1:2026-10-23"]["breaker"] == "would trip"


async def test_a_second_sweep_of_the_day_replaces_the_snapshot_and_keeps_the_counters(counters):
    await counters.add("acc-1", DAY, {"expiry_tool_errors": 2})
    await counters.record_sweep("acc-1", DAY, snapshot(due=4, would_stale=3))
    await counters.record_sweep("acc-1", DAY, snapshot(due=5, would_stale=1, breaker="ok"))

    (row,) = await counters.for_day(DAY)
    assert (row.due, row.would_stale, row.breaker) == (5, 1, "ok")      # the last run of the day wins, never a sum
    assert row.expiry_tool_errors == 2                                   # the tool's counter survives a snapshot


async def test_add_increments_and_concurrent_adds_are_not_lost(counters):
    await asyncio.gather(*[counters.add("acc-1", DAY, {"expiry_tool_errors": 1}) for _ in range(5)])
    await counters.add("acc-1", DAY, {"expiry_tool_errors": 3})

    (row,) = await counters.for_day(DAY)
    assert row.expiry_tool_errors == 8


async def test_an_add_before_any_sweep_makes_a_row_with_zero_snapshot_numbers(counters):
    await counters.add("acc-1", DAY, {"expiry_tool_errors": 1})

    (row,) = await counters.for_day(DAY)
    assert row == FactLifecycleDay(account_id="acc-1", day=DAY, expiry_tool_errors=1)


async def test_an_add_of_nothing_writes_nothing(counters, db, env):
    await counters.add("acc-1", DAY, {})
    await counters.add("acc-1", DAY, {"expiry_tool_errors": 0})

    assert db.collection(env.fact_lifecycle_daily_collection).docs == {}


async def test_for_day_returns_only_that_day_ordered_by_account(counters):
    await counters.record_sweep("acc-b", DAY, snapshot(due=2))
    await counters.record_sweep("acc-a", DAY, snapshot(due=1))
    await counters.record_sweep("acc-a", NEXT_DAY, snapshot(due=9))

    assert [(r.account_id, r.due) for r in await counters.for_day(DAY)] == [("acc-a", 1), ("acc-b", 2)]
    assert [(r.account_id, r.due) for r in await counters.for_day(NEXT_DAY)] == [("acc-a", 9)]
    assert await counters.for_day(date(2026, 10, 22)) == []


async def test_a_stored_row_reads_back_with_its_types(counters):
    await counters.record_sweep("acc-1", DAY, snapshot(oldest_stale_days=None))

    (row,) = await counters.for_day(DAY)
    assert row.run_at == RUN_AT and row.oldest_stale_days is None
    assert (row.due, row.newly_due, row.no_decision, row.staled, row.would_stale, row.stale_conflicts, row.backlog) == (
        4, 3, 1, 0, 3, 0, 2)


async def test_an_unreadable_row_is_skipped_not_fatal(counters, db, env):
    await counters.record_sweep("acc-1", DAY, snapshot())
    db.collection(env.fact_lifecycle_daily_collection).docs["broken:2026-10-23"] = {"account_id": "x", "day": "not-a-date"}

    assert [r.account_id for r in await counters.for_day(DAY)] == ["acc-1"]


async def test_claim_line_is_true_once_per_day(counters):
    assert await counters.claim_line(DAY) is True
    assert await counters.claim_line(DAY) is False
    assert await counters.claim_line(NEXT_DAY) is True


async def test_a_claimed_line_lives_in_its_own_collection_and_never_appears_in_for_day(counters, db, env):
    await counters.record_sweep("acc-1", DAY, snapshot())
    await counters.claim_line(DAY)

    assert list(db.collection(env.fact_lifecycle_lines_collection).docs) == ["2026-10-23"]
    assert list(db.collection(env.fact_lifecycle_daily_collection).docs) == ["acc-1:2026-10-23"]
    assert [r.account_id for r in await counters.for_day(DAY)] == ["acc-1"]


async def test_a_claim_alone_makes_no_account_row(counters):
    await counters.claim_line(DAY)

    assert await counters.for_day(DAY) == []


async def test_nothing_stored_has_a_field_named_expires_at(counters, db, env):
    await counters.record_sweep("acc-1", DAY, snapshot())
    await counters.add("acc-1", DAY, {"expiry_tool_errors": 1})
    await counters.claim_line(DAY)

    for name in (env.fact_lifecycle_daily_collection, env.fact_lifecycle_lines_collection):
        for doc in db.collection(name).docs.values():
            assert "expires_at" not in doc


@pytest.mark.parametrize("call", [
    lambda c: c.record_sweep("", DAY, snapshot()),
    lambda c: c.record_sweep("acc-1", datetime(2026, 10, 23, 7, 0), snapshot()),
    lambda c: c.record_sweep("acc-1", DAY, {"expires_at": RUN_AT}),
    lambda c: c.record_sweep("acc-1", DAY, {"expiry_tool_errors": 0}),
    lambda c: c.add("", DAY, {"expiry_tool_errors": 1}),
    lambda c: c.add("acc-1", DAY, {"due": 1}),
    lambda c: c.add("acc-1", DAY, {"expiry_tool_errors": -1}),
    lambda c: c.add("acc-1", DAY, {"expiry_tool_errors": 1.5}),
    lambda c: c.add("acc-1", DAY, {"expiry_tool_errors": True}),
], ids=["no-account", "datetime-day", "ttl-field", "counter-in-snapshot", "add-no-account", "snapshot-in-add",
        "negative", "float", "bool"])
async def test_a_bad_argument_is_refused_before_any_write(counters, db, env, call):
    with pytest.raises(ValueError):
        await call(counters)

    assert db.collection(env.fact_lifecycle_daily_collection).docs == {}


def test_the_two_field_sets_do_not_overlap_and_name_no_ttl_field():
    assert not (SNAPSHOT_FIELDS & COUNTER_FIELDS)
    assert "expires_at" not in SNAPSHOT_FIELDS | COUNTER_FIELDS


def test_the_collections_carry_the_environment_prefix_and_are_not_the_fact_collections():
    dev = EnvironmentConfig()
    assert dev.fact_lifecycle_daily_collection == f"{dev.firestore_collection_prefix}fact_lifecycle_daily"
    assert dev.fact_lifecycle_lines_collection == f"{dev.firestore_collection_prefix}fact_lifecycle_lines"
    assert dev.fact_lifecycle_daily_collection != dev.fact_lifecycle_lines_collection
    assert dev.domain_facts_collection not in (dev.fact_lifecycle_daily_collection, dev.fact_lifecycle_lines_collection)

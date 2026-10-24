"""Facts S4 · T4b (S1–S3 inward review L1): the janitor writes a typed snapshot with `review_enqueued=0`, and the row it
reports is the one the store reads back."""
from dataclasses import fields

from src.domain.fact_lifecycle_day import CounterField, SweepSnapshot
from tests.support.fact_kit import ACC
from tests.support.fact_lifecycle_world import NOW, JanitorWorld

DAY = NOW.date()


async def test_the_stored_snapshot_is_the_typed_field_set_with_no_review_enqueued():
    world = JanitorWorld(dry_run=True)
    await world.seed_due("x")
    await world.service.sweep()

    doc = world.daily_docs()[f"{ACC}:{DAY.isoformat()}"]
    assert set(doc) == {f.name for f in fields(SweepSnapshot)} | {"account_id", "day"}
    assert doc["review_enqueued"] == 0 and doc["run_at"] == NOW and doc["would_stale"] == 1


async def test_the_counters_survive_the_snapshot_and_read_back_typed():
    world = JanitorWorld(dry_run=True)
    await world.counters.add(ACC, DAY, {CounterField.REVIEWED: 2})
    await world.seed_due("x")
    await world.service.sweep()

    [row] = await world.counters.for_day(DAY)
    assert (row.reviewed, row.review_enqueued, row.due, row.would_stale, row.breaker) == (2, 0, 1, 1, "ok")

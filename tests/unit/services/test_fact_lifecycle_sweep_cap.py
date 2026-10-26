"""Facts S3 · F1 (owner, 2026-10-23): one run stales at most `BREAKER_BACKLOG - backlog` facts per account, oldest expiry
first; the rest are `deferred`. Log-only counts `would_stale` for all and reports what the cap would hold back.

In S3 no review exists (`last_review_at=None`), so any backlog trips the breaker. The tests that need a backlog without a
tripped breaker give the service the S4 condition, a review one hour old, by wrapping `breaker_reason`.
"""
import logging
from datetime import timedelta

import pytest

from src.domain import fact_lifecycle_sweep as sweep_rules
from tests.support.fact_kit import ACC, OTHER
from tests.support.fact_lifecycle_world import NOW, PAST, JanitorWorld

DAY = NOW.date()


@pytest.fixture
def recent_review(monkeypatch):
    real = sweep_rules.breaker_reason      # T9p: `plan_sweep` calls the breaker, so the seam is the domain module
    monkeypatch.setattr(sweep_rules, "breaker_reason", lambda count, last, now: real(count, now - timedelta(hours=1), now))


def daily(world, account=ACC):
    return world.daily_docs()[f"{account}:{DAY.isoformat()}"]


def alerts(world):
    return [t for t in world.sink.texts if t.startswith("🚨")]


async def seed_ordered(world, count, *, account=ACC, prefix="f"):
    """`count` due facts, fact i expiring i hours after the oldest: a higher i is a newer expiry."""
    base = NOW - timedelta(days=10)
    return [await world.seed_due(f"{prefix}{i:03d}", expires_at=base + timedelta(hours=i), account=account) for i in range(count)]


async def seed_backlog(world, count, *, account=ACC):
    for i in range(count):
        await world.seed(f"stale{i:03d}", state="stale", stale_since=PAST, account=account)


def states(world, ids):
    return [world.docs[i]["state"] for i in ids]


async def test_sixty_due_with_no_backlog_stales_the_fifty_oldest_and_defers_ten():
    world = JanitorWorld()
    ids = await seed_ordered(world, 60)

    result = await world.service.sweep()

    assert states(world, ids[:50]) == ["stale"] * 50 and states(world, ids[50:]) == ["current"] * 10
    assert (result["staled"], result["deferred"], result["conflicts"], result["breaker_tripped"]) == (50, 10, 0, 0)
    snap = daily(world)
    assert (snap["staled"], snap["deferred"], snap["would_stale"], snap["breaker"]) == (50, 10, 0, "ok")
    assert alerts(world) == []
    assert "staled 50 / would stale 0 / deferred 10" in world.sink.texts[0]


async def test_the_cap_applies_per_account():
    world = JanitorWorld()
    a = await seed_ordered(world, 55, account=ACC, prefix="a")
    b = await seed_ordered(world, 5, account=OTHER, prefix="b")

    result = await world.service.sweep()

    assert (result["staled"], result["deferred"]) == (55, 5)
    assert states(world, b) == ["stale"] * 5 and states(world, a).count("stale") == 50
    assert (daily(world, OTHER)["deferred"], daily(world)["deferred"]) == (0, 5)


async def test_the_deferred_facts_are_the_ones_with_the_newest_expiry_not_the_highest_ids():
    world = JanitorWorld()
    base = NOW - timedelta(days=5)
    ids = [await world.seed_due(f"r{i:03d}", expires_at=base - timedelta(hours=i)) for i in range(52)]   # r000 expires last

    await world.service.sweep()

    assert states(world, ids[:2]) == ["current"] * 2 and states(world, ids[2:]) == ["stale"] * 50


async def test_equal_expiry_is_cut_by_fact_id():
    world = JanitorWorld()
    ids = [await world.seed_due(f"t{i:03d}", expires_at=PAST) for i in range(52)]

    await world.service.sweep()

    assert states(world, sorted(ids)[:50]) == ["stale"] * 50 and states(world, sorted(ids)[50:]) == ["current"] * 2


async def test_a_backlog_of_45_with_a_recent_review_leaves_headroom_for_five(recent_review):
    world = JanitorWorld()
    await seed_backlog(world, 45)
    ids = await seed_ordered(world, 10)

    result = await world.service.sweep()

    assert states(world, ids) == ["stale"] * 5 + ["current"] * 5
    assert (result["staled"], result["deferred"], result["breaker_tripped"]) == (5, 5, 0)
    assert (daily(world)["backlog"], daily(world)["breaker"]) == (45, "ok") and alerts(world) == []


async def test_a_conflict_inside_the_cap_does_not_use_headroom(recent_review):
    world = JanitorWorld()
    await seed_backlog(world, 45)
    ids = await seed_ordered(world, 8)
    world.store.interfere(ids[0], {"last_updated": NOW})                      # the oldest conflicts

    result = await world.service.sweep()

    assert states(world, ids) == ["current"] + ["stale"] * 5 + ["current"] * 2
    assert (result["staled"], result["conflicts"], result["deferred"]) == (5, 1, 2)


async def test_exactly_fifty_in_the_backlog_does_not_trip_but_leaves_no_headroom(recent_review):
    world = JanitorWorld()
    await seed_backlog(world, 50)
    ids = await seed_ordered(world, 3)
    await world.seed("old-writer", expires_at=PAST, expiry_kind=None)

    result = await world.service.sweep()

    assert states(world, ids) == ["current"] * 3
    assert (result["staled"], result["deferred"], result["no_decision"], result["breaker_tripped"]) == (0, 3, 1, 0)
    assert alerts(world) == [] and daily(world)["breaker"] == "ok"


async def test_fifty_one_in_the_backlog_trips_and_defers_nothing(recent_review):
    world = JanitorWorld()
    await seed_backlog(world, 51)
    ids = await seed_ordered(world, 3)

    result = await world.service.sweep()

    assert states(world, ids) == ["current"] * 3
    assert (result["staled"], result["deferred"], result["breaker_tripped"]) == (0, 0, 1)
    (alert,) = alerts(world)
    assert "3 due facts were not staled" in alert


async def test_a_tripped_breaker_defers_nothing():
    world = JanitorWorld()
    await seed_backlog(world, 1)
    await seed_ordered(world, 60)

    result = await world.service.sweep()

    assert (result["staled"], result["deferred"], result["breaker_tripped"]) == (0, 0, 1)
    assert daily(world)["deferred"] == 0


async def test_log_only_counts_would_stale_for_all_and_reports_what_the_cap_would_hold_back():
    world = JanitorWorld(dry_run=True)
    ids = await seed_ordered(world, 60)
    await world.seed("old-writer", expires_at=PAST, expiry_kind=None)
    before = {k: dict(v) for k, v in world.docs.items()}

    result = await world.service.sweep()

    assert world.docs == before and states(world, ids) == ["current"] * 60
    assert (result["would_stale"], result["deferred"], result["staled"], result["no_decision"]) == (60, 10, 0, 1)
    snap = daily(world)
    assert (snap["would_stale"], snap["deferred"], snap["staled"]) == (60, 10, 0)
    assert "staled 0 / would stale 60 / deferred 10" in world.sink.texts[0]


async def test_log_only_with_a_backlog_of_45_reports_five_of_ten_held_back(recent_review):
    world = JanitorWorld(dry_run=True)
    await seed_backlog(world, 45)
    await seed_ordered(world, 10)

    result = await world.service.sweep()

    assert (result["would_stale"], result["deferred"]) == (10, 5)


async def test_log_only_with_a_tripped_breaker_defers_nothing_and_counts_every_fact():
    world = JanitorWorld(dry_run=True)
    await seed_backlog(world, 1)
    await seed_ordered(world, 60)

    result = await world.service.sweep()

    assert (result["would_stale"], result["deferred"], result["breaker_tripped"]) == (60, 0, 1)
    assert daily(world)["breaker"] == "would trip"


async def test_the_cache_of_a_capped_account_is_refreshed_and_shows_only_the_deferred_facts():
    world = JanitorWorld()
    await seed_ordered(world, 52)
    await world.repo.refresh_biographical_context_cache(ACC)

    await world.service.sweep()

    cached = {i["text"] for i in await world.repo.get_biographical_context_cached(ACC)}
    assert cached == {"f050", "f051"}


async def test_the_log_line_carries_deferred_after_would_stale(caplog):
    caplog.set_level(logging.INFO)
    world = JanitorWorld()
    await seed_ordered(world, 52)

    await world.service.sweep()

    (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[FactLifecycle] account=")]
    assert line == (
        "[FactLifecycle] account=acc-1 due=52 newly_due=52 no_decision=0 staled=50 would_stale=0 deferred=2 conflicts=0 "
        "backlog=0 breaker=ok review_enqueued=0 dry_run=False"
    )


async def test_a_second_run_after_a_capped_one_trips_the_breaker_in_s3_and_stales_nothing_more():
    world = JanitorWorld()
    ids = await seed_ordered(world, 60)
    await world.service.sweep()

    world.now = NOW + timedelta(hours=3)
    result = await world.service.sweep()

    assert states(world, ids).count("stale") == 50 and (result["staled"], result["breaker_tripped"]) == (0, 1)

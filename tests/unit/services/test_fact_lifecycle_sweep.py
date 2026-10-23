"""Facts S3 · T9: the janitor service on the evaluating fact store and the real counters adapter (plan §3.14-§3.17).

What is asserted is what the store holds and what the sink received, never a call.
"""
from datetime import timedelta

import pytest

from src.domain.fact_lifecycle_day import SNAPSHOT_FIELDS
from tests.support.fact_kit import ACC, OTHER, make_fact
from tests.support.fact_lifecycle_world import FUTURE, NOW, PAST, JanitorWorld

DAY = NOW.date()
YESTERDAY = DAY - timedelta(days=1)
EMPTY_RUN = {"accounts": 0, "due": 0, "staled": 0, "would_stale": 0, "conflicts": 0, "no_decision": 0,
             "breaker_tripped": 0, "dry_run": True}


def snapshot_of(world, account=ACC):
    return world.daily_docs()[f"{account}:{DAY.isoformat()}"]


async def cached_texts(world, account=ACC):
    return {item["text"] for item in await world.repo.get_biographical_context_cached(account)}


# ----------------------------------------------------------------------------------------------- what it stales

async def test_an_empty_store_returns_the_zero_totals_and_posts_nothing():
    world = JanitorWorld(dry_run=True)
    assert await world.service.sweep() == EMPTY_RUN
    assert world.sink.texts == [] and world.daily_docs() == {} and world.line_docs() == {}


async def test_it_stales_every_due_current_fact_of_every_account_and_nothing_else():
    world = JanitorWorld()
    due_a = await world.seed_due("due-a")
    due_b = await world.seed_due("due-b", account=OTHER)
    later = await world.seed("later", expires_at=FUTURE)
    pinned = await world.seed("pinned", expiry_kind="none")
    closed = await world.seed_due("closed", state="archived")
    before = {k: dict(v) for k, v in world.docs.items() if k in (later, pinned, closed)}

    result = await world.service.sweep()

    assert result == {"accounts": 2, "due": 2, "staled": 2, "would_stale": 0, "conflicts": 0, "no_decision": 0,
                      "breaker_tripped": 0, "dry_run": False}
    for fact_id in (due_a, due_b):
        doc = world.docs[fact_id]
        assert doc["state"] == "stale" and doc["stale_since"] == NOW and doc["last_updated"] == NOW
        assert doc["lifecycle_events"][-1]["actor"] == "janitor" and doc["lifecycle_events"][-1]["to"] == "stale"
        assert doc["expires_at"] == PAST and doc["expiry_kind"] == "date"
    assert {k: world.docs[k] for k in before} == before


async def test_a_run_is_idempotent_the_next_one_finds_nothing_due_and_adds_one_event_only_once():
    world = JanitorWorld()
    fact_id = await world.seed_due("x")
    await world.service.sweep()
    events = list(world.docs[fact_id]["lifecycle_events"])

    world.now = NOW + timedelta(hours=2)
    second = await world.service.sweep()

    assert (second["due"], second["staled"]) == (0, 0)
    assert world.docs[fact_id]["lifecycle_events"] == events and world.docs[fact_id]["stale_since"] == NOW


async def test_every_fact_of_a_long_due_list_is_handled():
    world = JanitorWorld()
    ids = [await world.seed_due(f"f{i}", account=ACC if i % 2 else OTHER) for i in range(120)]

    result = await world.service.sweep()

    assert (result["due"], result["staled"], result["accounts"]) == (120, 120, 2)
    assert {world.docs[i]["state"] for i in ids} == {"stale"}


async def test_a_due_fact_without_a_decision_is_skipped_and_counted_never_staled():
    world = JanitorWorld()
    keep = await world.seed("old-writer", expires_at=PAST, expiry_kind=None)
    gone = await world.seed_due("decided")

    result = await world.service.sweep()

    assert world.docs[keep]["state"] == "current" and world.docs[gone]["state"] == "stale"
    assert (result["due"], result["staled"], result["no_decision"]) == (2, 1, 1)
    snap = snapshot_of(world)
    assert (snap["due"], snap["staled"], snap["no_decision"]) == (2, 1, 1)


async def test_a_conflict_skips_that_fact_counts_it_and_the_run_goes_on():
    world = JanitorWorld()
    contested = await world.seed_due("contested")
    other = await world.seed_due("other")
    world.store.interfere(contested, {"last_updated": NOW})

    result = await world.service.sweep()

    assert world.docs[contested]["state"] == "current" and world.docs[other]["state"] == "stale"
    assert (result["staled"], result["conflicts"]) == (1, 1)
    assert snapshot_of(world)["stale_conflicts"] == 1
    assert await cached_texts(world) == {"contested"}                      # the cache was refreshed for the one staled


    world.now = NOW + timedelta(hours=1)
    again = await world.service.sweep()
    # S3 has no review, so the stale fact of the first run now trips the breaker: the contested fact waits (plan §3.14)
    assert world.docs[contested]["state"] == "current" and (again["staled"], again["breaker_tripped"]) == (0, 1)


async def test_an_error_other_than_a_conflict_fails_the_run_loudly_after_the_earlier_facts_reach_the_cache():
    world = JanitorWorld()
    first = await world.seed_due("a-first")
    broken = await world.seed_due("b-broken")
    await world.repo.refresh_biographical_context_cache(ACC)
    assert await cached_texts(world) == {"a-first", "b-broken"}
    world.repo.fail_on = {broken}
    world.docs[first]["expires_at"] = PAST - timedelta(days=1)             # the fake orders a range query by this field: first goes first

    with pytest.raises(RuntimeError, match="store unavailable"):
        await world.service.sweep()

    assert world.docs[first]["state"] == "stale" and world.docs[broken]["state"] == "current"
    assert await cached_texts(world) == {"b-broken"}                       # what was staled left the prompts before the run failed
    assert world.daily_docs() == {} and world.sink.texts == [] and world.line_docs() == {}


async def test_a_stale_conflict_is_not_confused_with_other_errors_staling_continues_past_it():
    world = JanitorWorld()
    ids = [await world.seed_due(f"f{i}") for i in range(5)]
    world.store.interfere(ids[2], {"last_updated": NOW})
    result = await world.service.sweep()
    assert (result["staled"], result["conflicts"]) == (4, 1)


# ----------------------------------------------------------------------------------------------- the cache

async def test_the_cache_is_refreshed_only_for_accounts_that_staled_something():
    world = JanitorWorld()
    gone = await world.seed_due("gone")
    kept_a = await world.seed("kept-a")
    kept_b = await world.seed("kept-b", account=OTHER)
    for account in (ACC, OTHER):
        await world.repo.refresh_biographical_context_cache(account)
    world.docs[kept_b]["text"] = "kept-b (edited behind the cache)"          # a refresh of OTHER would show this

    await world.service.sweep()

    assert await cached_texts(world) == {"kept-a"}
    assert await cached_texts(world, OTHER) == {"kept-b"}
    assert world.docs[gone]["state"] == "stale" and world.docs[kept_a]["state"] == "current"


async def test_log_only_never_touches_the_facts_or_the_cache():
    world = JanitorWorld(dry_run=True)
    await world.seed_due("due")
    await world.repo.refresh_biographical_context_cache(ACC)
    facts_before = {k: dict(v) for k, v in world.docs.items()}
    cache_before = world.store.db.collection("eval_user_context").docs[ACC]

    result = await world.service.sweep()

    assert world.docs == facts_before
    assert world.store.db.collection("eval_user_context").docs[ACC] == cache_before
    assert (result["staled"], result["would_stale"], result["dry_run"]) == (0, 1, True)


# ----------------------------------------------------------------------------------------------- the breaker

async def test_the_breaker_trips_on_stale_facts_with_no_review_and_stales_nothing_but_alerts_once_per_account():
    world = JanitorWorld()
    await world.seed("already-stale", state="stale", stale_since=PAST)
    due = await world.seed_due("secret-due")
    other = await world.seed_due("other-account", account=OTHER)

    result = await world.service.sweep()

    assert world.docs[due]["state"] == "current"
    assert world.docs[other]["state"] == "stale"                            # no stale backlog there: not tripped
    assert (result["staled"], result["breaker_tripped"]) == (1, 1)
    alerts = [t for t in world.sink.texts if t.startswith("🚨")]
    assert len(alerts) == 1 and ACC[:8] in alerts[0] and "breaker" in alerts[0]
    assert "secret" not in alerts[0] and "f-secret-due" not in alerts[0]
    assert snapshot_of(world)["breaker"] == "tripped" and snapshot_of(world, OTHER)["breaker"] == "ok"


async def test_a_tripped_breaker_alerts_again_on_every_run_but_the_line_only_once():
    world = JanitorWorld()
    await world.seed("already-stale", state="stale", stale_since=PAST)
    await world.seed_due("due")

    await world.service.sweep()
    world.now = NOW + timedelta(hours=3)
    await world.service.sweep()

    alerts = [t for t in world.sink.texts if t.startswith("🚨")]
    lines = [t for t in world.sink.texts if t.startswith("🧹")]
    assert len(alerts) == 2 and len(lines) == 1


async def test_log_only_counts_would_stale_behind_a_tripped_breaker_and_reports_it_without_an_alert():
    world = JanitorWorld(dry_run=True)
    await world.seed("already-stale", state="stale", stale_since=PAST)
    due = await world.seed_due("due")
    await world.seed("old-writer", expires_at=PAST, expiry_kind=None)

    result = await world.service.sweep()

    assert world.docs[due]["state"] == "current"
    assert (result["would_stale"], result["staled"], result["no_decision"], result["breaker_tripped"]) == (1, 0, 1, 1)
    snap = snapshot_of(world)
    assert (snap["would_stale"], snap["staled"], snap["breaker"], snap["backlog"]) == (1, 0, "would trip", 1)
    assert [t for t in world.sink.texts if t.startswith("🚨")] == []
    assert "breaker would trip" in world.sink.texts[0] and "· log-only" in world.sink.texts[0]


async def test_an_account_with_only_a_stale_backlog_gets_a_snapshot_with_its_age():
    world = JanitorWorld(dry_run=True)
    await world.seed("old", state="stale", stale_since=NOW - timedelta(days=11, hours=3))
    await world.seed("unknown-age", state="stale")
    await world.seed("newer", state="stale", stale_since=NOW - timedelta(days=2))

    await world.service.sweep()

    snap = snapshot_of(world)
    assert (snap["due"], snap["backlog"], snap["oldest_stale_days"]) == (0, 3, 11)


async def test_a_backlog_with_no_known_age_stores_a_null_age():
    world = JanitorWorld(dry_run=True)
    await world.seed("unknown-age", state="stale")
    await world.service.sweep()
    assert snapshot_of(world)["oldest_stale_days"] is None and snapshot_of(world)["backlog"] == 1


# ----------------------------------------------------------------------------------------------- the snapshot

async def test_the_snapshot_always_carries_the_whole_field_set_oldest_age_none_included():
    world = JanitorWorld()
    await world.seed_due("x")
    await world.service.sweep()
    snap = snapshot_of(world)
    assert set(snap) == SNAPSHOT_FIELDS | {"account_id", "day"}
    assert snap["oldest_stale_days"] is None and snap["run_at"] == NOW and snap["breaker"] == "ok"


async def test_the_snapshot_is_written_in_log_only_mode_too():
    world = JanitorWorld(dry_run=True)
    await world.seed_due("x")
    await world.seed("y", expires_at=PAST, expiry_kind=None)
    await world.service.sweep()
    snap = snapshot_of(world)
    assert (snap["due"], snap["newly_due"], snap["would_stale"], snap["no_decision"], snap["staled"]) == (2, 2, 1, 1, 0)


async def test_a_second_run_of_the_day_replaces_the_snapshot_keeps_the_tool_errors_and_posts_nothing_more():
    world = JanitorWorld(dry_run=True)
    await world.seed_due("first")
    await world.counters.add(ACC, DAY, {"expiry_tool_errors": 4})
    await world.service.sweep()
    assert len(world.sink.texts) == 1

    world.now = NOW + timedelta(hours=5)
    await world.seed_due("second")
    await world.service.sweep()

    snap = snapshot_of(world)
    assert (snap["due"], snap["would_stale"], snap["run_at"]) == (2, 2, world.now)
    assert snap["expiry_tool_errors"] == 4
    assert len(world.sink.texts) == 1


async def test_a_second_run_with_nothing_new_replaces_the_figures_with_that_runs_even_zeros():
    world = JanitorWorld()
    await world.seed("already-stale", state="stale", stale_since=PAST)
    await world.seed_due("blocked")
    await world.service.sweep()
    assert snapshot_of(world)["oldest_stale_days"] == 2
    world.docs["f-already-stale"].pop("stale_since")
    world.docs["f-blocked"]["state"] = "archived"

    world.now = NOW + timedelta(hours=1)
    await world.service.sweep()

    snap = snapshot_of(world)
    assert (snap["due"], snap["breaker"], snap["oldest_stale_days"]) == (0, "tripped", None)


# ----------------------------------------------------------------------------------------------- newly due

async def test_newly_due_counts_facts_that_fell_due_after_the_previous_run_of_the_day():
    world = JanitorWorld(dry_run=True)
    await world.seed_due("old", expires_at=NOW - timedelta(days=3))
    await world.service.sweep()
    assert snapshot_of(world)["newly_due"] == 1

    world.now = NOW + timedelta(hours=6)
    await world.seed_due("fresh", expires_at=NOW + timedelta(hours=1))
    await world.service.sweep()

    snap = snapshot_of(world)
    assert (snap["due"], snap["newly_due"]) == (2, 1)


async def test_newly_due_falls_back_to_yesterdays_run_when_today_has_none():
    world = JanitorWorld(dry_run=True)
    await world.seed_due("old", expires_at=NOW - timedelta(days=3))
    await world.service.sweep()

    world.now = NOW + timedelta(days=1)
    await world.seed_due("fresh", expires_at=NOW + timedelta(hours=2))
    await world.service.sweep()

    snap = world.daily_docs()[f"{ACC}:{(DAY + timedelta(days=1)).isoformat()}"]
    assert (snap["due"], snap["newly_due"]) == (2, 1)


async def test_without_any_previous_run_every_due_fact_is_new():
    world = JanitorWorld(dry_run=True)
    for i in range(3):
        await world.seed_due(f"f{i}", expires_at=NOW - timedelta(days=30 + i))
    await world.service.sweep()
    assert snapshot_of(world)["newly_due"] == 3


# ----------------------------------------------------------------------------------------------- the ops line

async def test_the_line_is_posted_after_the_run_with_that_runs_numbers_once_the_snapshots_are_stored():
    world = JanitorWorld()
    await world.seed_due("a")
    await world.seed_due("b")
    seen = {}

    async def look():
        seen["snapshot"] = dict(snapshot_of(world))
        seen["facts"] = sorted(world.docs[i]["state"] for i in ("f-a", "f-b"))
        seen["claimed"] = dict(world.line_docs())

    world.sink.at_post = look
    await world.service.sweep()

    assert seen["facts"] == ["stale", "stale"] and seen["snapshot"]["staled"] == 2
    assert DAY.isoformat() in seen["claimed"]                              # claimed before the post (see the comment in the service)
    (line,) = world.sink.texts
    assert line == (
        "🧹 Fact lifecycle — run 2026-10-23 07:30 UTC\n"
        "acc-1: due 2 (new 2, no decision 0) | staled 2 / would stale 0 | conflicts 0 | breaker ok"
        " | backlog 0 | expiry tool errors yesterday 0"
    )


async def test_the_line_shows_yesterdays_expiry_tool_errors_not_todays():
    world = JanitorWorld()
    await world.seed_due("a")
    await world.counters.add(ACC, YESTERDAY, {"expiry_tool_errors": 3})
    await world.counters.add(ACC, DAY, {"expiry_tool_errors": 9})

    await world.service.sweep()

    assert "expiry tool errors yesterday 3" in world.sink.texts[0]


async def test_an_account_with_only_yesterdays_tool_errors_still_gets_a_line():
    world = JanitorWorld()
    await world.counters.add(OTHER, YESTERDAY, {"expiry_tool_errors": 2})

    result = await world.service.sweep()

    assert result["accounts"] == 0
    (line,) = world.sink.texts
    assert line.splitlines()[1].startswith(f"{OTHER[:8]}: due 0") and line.endswith("expiry tool errors yesterday 2")


async def test_a_quiet_run_posts_nothing_and_does_not_use_up_the_days_line():
    world = JanitorWorld()
    await world.seed("not-due", expires_at=FUTURE)
    await world.service.sweep()
    assert world.sink.texts == [] and world.line_docs() == {}

    await world.seed_due("due-later-in-the-day")
    world.now = NOW + timedelta(hours=4)
    await world.service.sweep()
    assert len(world.sink.texts) == 1


async def test_a_failed_post_loses_the_days_line_but_never_the_run_or_the_snapshot():
    world = JanitorWorld()
    fact = await world.seed_due("due")
    world.sink.fail = True

    result = await world.service.sweep()                                    # does not raise

    assert result["staled"] == 1 and world.docs[fact]["state"] == "stale"
    assert snapshot_of(world)["staled"] == 1
    assert DAY.isoformat() in world.line_docs()                             # claim before post: no second attempt
    world.sink.fail = False
    world.now = NOW + timedelta(hours=1)
    await world.seed_due("another")
    await world.service.sweep()
    assert [t for t in world.sink.texts if t.startswith("🧹")] == []


async def test_a_failed_breaker_alert_does_not_fail_the_run():
    world = JanitorWorld()
    await world.seed("already-stale", state="stale", stale_since=PAST)
    await world.seed_due("due")
    world.sink.fail = True
    result = await world.service.sweep()
    assert result["breaker_tripped"] == 1 and snapshot_of(world)["breaker"] == "tripped"


async def test_without_a_sink_the_run_works_and_claims_nothing():
    world = JanitorWorld(with_sink=False)
    fact = await world.seed_due("due")
    await world.service.sweep()
    assert world.docs[fact]["state"] == "stale" and world.line_docs() == {}


async def test_a_claim_that_errors_posts_nothing_and_does_not_fail_the_run(monkeypatch):
    world = JanitorWorld()
    await world.seed_due("due")

    async def broken(day):
        raise RuntimeError("store down")

    monkeypatch.setattr(world.counters, "claim_line", broken)
    result = await world.service.sweep()
    assert result["staled"] == 1 and world.sink.texts == []


async def test_a_line_never_carries_fact_text_or_ids():
    world = JanitorWorld()
    await world.seed("already-stale secret-diagnosis", state="stale", stale_since=PAST)
    await world.seed_due("secret-medication")
    await world.service.sweep()
    joined = "\n".join(world.sink.texts)
    assert "secret" not in joined and "f-secret" not in joined

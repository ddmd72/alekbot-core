"""Facts S3 · T9 review fixes (F2, F3, F5): the breaker alert needs a held-back fact, the survivors of the mutant review, and a
failed run that is logged and alerted before it is re-raised."""
import logging
from datetime import timedelta

import pytest

from src.domain.fact_lifecycle_sweep import breaker_reason
from tests.support.fact_kit import ACC, OTHER
from tests.support.fact_lifecycle_world import FUTURE, NOW, PAST, JanitorWorld

DAY = NOW.date()


def daily(world, account=ACC, day=DAY):
    return world.daily_docs()[f"{account}:{day.isoformat()}"]


def alerts(world):
    return [t for t in world.sink.texts if t.startswith("🚨")]


def lifecycle_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("[FactLifecycle] account=")]


# ------------------------------------------------------------------------------------------------ F2

async def test_a_tripped_account_with_only_a_stale_backlog_and_nothing_due_gets_no_alert_but_reports_tripped():
    world = JanitorWorld()
    await world.seed("already-stale", state="stale", stale_since=PAST)

    result = await world.service.sweep()

    assert result["breaker_tripped"] == 1 and alerts(world) == []
    assert daily(world)["breaker"] == "tripped"
    assert "breaker tripped" in world.sink.texts[0] and world.sink.texts[0].startswith("🧹")


async def test_a_tripped_account_whose_due_facts_all_lack_a_decision_gets_no_alert():
    world = JanitorWorld()
    await world.seed("already-stale", state="stale", stale_since=PAST)
    await world.seed("old-writer", expires_at=PAST, expiry_kind=None)

    await world.service.sweep()

    assert alerts(world) == [] and daily(world)["breaker"] == "tripped" and daily(world)["no_decision"] == 1


async def test_a_tripped_account_with_a_held_back_fact_alerts_with_the_count():
    world = JanitorWorld()
    await world.seed("already-stale", state="stale", stale_since=PAST)
    await world.seed_due("d1")
    await world.seed_due("d2")
    await world.seed("old-writer", expires_at=PAST, expiry_kind=None)

    await world.service.sweep()

    (alert,) = alerts(world)
    assert "2 due facts were not staled" in alert


# ------------------------------------------------------------------------------------------------ F3 survivors

async def test_the_cache_of_an_account_in_the_run_that_staled_nothing_is_left_alone():
    world = JanitorWorld()
    await world.seed_due("gone")                                            # ACC: staled
    await world.seed("already-stale", state="stale", stale_since=PAST, account=OTHER)
    held = await world.seed_due("held-back", account=OTHER)                 # OTHER: in the run, tripped, stales nothing
    await world.repo.refresh_biographical_context_cache(OTHER)
    world.docs[held]["text"] = "edited behind the cache"                    # a refresh of OTHER would show this

    result = await world.service.sweep()

    assert (result["staled"], result["breaker_tripped"]) == (1, 1)
    assert {i["text"] for i in await world.repo.get_biographical_context_cached(OTHER)} == {"held-back"}


async def test_a_conflicting_only_fact_does_not_refresh_its_accounts_cache():
    world = JanitorWorld()
    contested = await world.seed_due("contested")
    await world.repo.refresh_biographical_context_cache(ACC)
    world.docs[contested]["text"] = "edited behind the cache"
    world.store.interfere(contested, {"last_updated": NOW})

    result = await world.service.sweep()

    assert (result["staled"], result["conflicts"]) == (0, 1)
    assert {i["text"] for i in await world.repo.get_biographical_context_cached(ACC)} == {"contested"}


async def test_a_fact_due_exactly_at_the_previous_runs_time_is_not_new():
    world = JanitorWorld(dry_run=True)
    await world.seed_due("first", expires_at=PAST)
    await world.service.sweep()                                             # run_at == NOW

    world.now = NOW + timedelta(hours=1)
    await world.seed_due("on-the-dot", expires_at=NOW)
    await world.seed_due("after", expires_at=NOW + timedelta(minutes=30))
    await world.service.sweep()

    assert (daily(world)["due"], daily(world)["newly_due"]) == (3, 1)


async def test_todays_run_wins_over_yesterdays_as_the_previous_run():
    world = JanitorWorld(dry_run=True)
    yesterday_run = NOW - timedelta(days=1)
    await world.seed_due("old", expires_at=yesterday_run - timedelta(days=1))
    await world.seed_due("between", expires_at=NOW - timedelta(hours=12))   # after yesterday's run, before today's
    await world.seed_due("later", expires_at=NOW + timedelta(hours=1))      # due only at the second run of today
    world.now = yesterday_run
    await world.service.sweep()
    world.now = NOW
    await world.service.sweep()
    assert daily(world)["newly_due"] == 1                                   # "between" is new against yesterday's run

    world.now = NOW + timedelta(hours=2)
    await world.service.sweep()

    assert (daily(world)["due"], daily(world)["newly_due"]) == (3, 1)       # against today's first run: only "later"


async def test_the_per_account_log_line_has_the_plan_format_in_write_mode(caplog):
    caplog.set_level(logging.INFO)
    world = JanitorWorld()
    await world.seed_due("a")
    await world.seed_due("b")
    await world.seed("old-writer", expires_at=PAST, expiry_kind=None)

    await world.service.sweep()

    assert lifecycle_lines(caplog) == [
        "[FactLifecycle] account=acc-1 due=3 newly_due=3 no_decision=1 staled=2 would_stale=0 conflicts=0 backlog=0 "
        "breaker=ok review_enqueued=0 dry_run=False"
    ]


async def test_the_per_account_log_line_in_log_only_mode_with_a_tripped_breaker(caplog):
    caplog.set_level(logging.INFO)
    world = JanitorWorld(dry_run=True)
    await world.seed("already-stale", state="stale", stale_since=PAST)
    await world.seed_due("a")

    await world.service.sweep()

    assert lifecycle_lines(caplog) == [
        "[FactLifecycle] account=acc-1 due=1 newly_due=1 no_decision=0 staled=0 would_stale=1 conflicts=0 backlog=1 "
        f"breaker={breaker_reason(1, None, NOW)} review_enqueued=0 dry_run=True"
    ]


async def test_one_alert_per_tripped_account():
    world = JanitorWorld()
    for account in (ACC, OTHER):
        await world.seed("already-stale", state="stale", stale_since=PAST, account=account, id=f"stale-{account}")
        await world.seed_due("due", account=account, id=f"due-{account}")

    result = await world.service.sweep()

    assert result["breaker_tripped"] == 2
    found = alerts(world)
    assert len(found) == 2 and sum(ACC[:8] in a for a in found) == 1 and sum(OTHER[:8] in a for a in found) == 1


# ------------------------------------------------------------------------------------------------ F5

async def test_a_failing_due_query_is_logged_alerted_once_and_reraised(monkeypatch, caplog):
    world = JanitorWorld()

    async def failing(now):
        raise RuntimeError("index still building")

    monkeypatch.setattr(world.repo, "find_due_facts", failing)

    with pytest.raises(RuntimeError, match="index still building"):
        await world.service.sweep()

    (alert,) = world.sink.texts
    assert alert.startswith("🚨") and "RuntimeError" in alert and "the due query" in alert
    assert "index still building" not in alert
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("RuntimeError" in r.getMessage() and "the due query" in r.getMessage() for r in errors)
    assert world.line_docs() == {} and world.daily_docs() == {}


async def test_a_failing_backlog_query_is_named_by_its_stage(monkeypatch):
    world = JanitorWorld()

    async def failing():
        raise ValueError("boom")

    monkeypatch.setattr(world.repo, "stale_backlog", failing)

    with pytest.raises(ValueError):
        await world.service.sweep()

    (alert,) = world.sink.texts
    assert "ValueError" in alert and "the stale backlog query" in alert


async def test_a_failing_stale_write_alerts_once_naming_the_account_and_the_facts_before_it_still_leave_the_cache():
    world = JanitorWorld()
    first = await world.seed_due("a-first", expires_at=PAST - timedelta(days=1))
    broken = await world.seed_due("b-broken")
    await world.repo.refresh_biographical_context_cache(ACC)
    world.repo.fail_on = {broken}

    with pytest.raises(RuntimeError, match="store unavailable"):
        await world.service.sweep()

    (alert,) = world.sink.texts
    assert "RuntimeError" in alert and "the account sweep" in alert and f"account {ACC[:8]}" in alert
    assert world.docs[first]["state"] == "stale" and world.docs[broken]["state"] == "current"
    assert {i["text"] for i in await world.repo.get_biographical_context_cached(ACC)} == {"b-broken"}
    assert world.line_docs() == {}


async def test_a_failing_alert_sink_during_a_failed_run_is_only_a_warning_and_the_original_error_surfaces(monkeypatch, caplog):
    world = JanitorWorld()
    world.sink.fail = True

    async def failing(now):
        raise RuntimeError("original failure")

    monkeypatch.setattr(world.repo, "find_due_facts", failing)

    with pytest.raises(RuntimeError, match="original failure"):
        await world.service.sweep()

    assert world.sink.texts == []
    assert any(r.levelno == logging.WARNING and "not delivered" in r.getMessage() for r in caplog.records)


async def test_a_failed_run_without_a_sink_still_logs_and_reraises(monkeypatch, caplog):
    world = JanitorWorld(with_sink=False)

    async def failing(now):
        raise RuntimeError("no sink")

    monkeypatch.setattr(world.repo, "find_due_facts", failing)

    with pytest.raises(RuntimeError, match="no sink"):
        await world.service.sweep()

    assert any(r.levelno >= logging.ERROR and "RuntimeError" in r.getMessage() for r in caplog.records)


async def test_a_conflict_is_not_a_failed_run():
    world = JanitorWorld()
    contested = await world.seed_due("contested")
    world.store.interfere(contested, {"last_updated": NOW})

    await world.service.sweep()

    assert [t for t in world.sink.texts if "run failed" in t] == []


# ------------------------------------------------------------------------------------------------ F5 follow-up: the alert is capped

async def test_a_hung_sink_cannot_hold_the_original_error(monkeypatch, caplog):
    import asyncio
    import time

    world = JanitorWorld()
    world.service.FAILURE_ALERT_TIMEOUT_S = 0.05

    async def hang():
        await asyncio.sleep(30)

    world.sink.at_post = hang

    async def failing(now):
        raise RuntimeError("original failure")

    monkeypatch.setattr(world.repo, "find_due_facts", failing)

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="original failure"):
        await world.service.sweep()

    assert time.monotonic() - started < 5
    assert world.sink.texts == []
    assert any(r.levelno == logging.WARNING and "was cut after" in r.getMessage() for r in caplog.records)


async def test_a_cancellation_during_the_failure_alert_is_not_swallowed(monkeypatch):
    import asyncio

    world = JanitorWorld()
    entered = asyncio.Event()

    async def hang():
        entered.set()
        await asyncio.sleep(30)

    world.sink.at_post = hang

    async def failing(now):
        raise RuntimeError("original failure")

    monkeypatch.setattr(world.repo, "find_due_facts", failing)

    task = asyncio.ensure_future(world.service.sweep())
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

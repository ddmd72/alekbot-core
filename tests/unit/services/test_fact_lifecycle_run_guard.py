"""Facts S4 · T9c (S4-D10): the janitor takes the run guard before its first read and frees it after, whatever happens.

The world is the real service over the evaluating store and the real counters adapter; what is asserted is the guard
document, the facts and the sink, never a call.
"""
import logging
from datetime import timedelta

import pytest

from tests.support.fact_lifecycle_world import FUTURE, NOW, JanitorWorld

BUSY = {"status": "busy"}


def guard_docs(world):
    return world.store.db.collection(world.env.fact_lifecycle_run_collection).docs


def state_of(world, fact_id):
    return world.docs[fact_id]["state"]


async def test_a_run_that_meets_a_held_guard_does_nothing_and_says_busy():
    world = JanitorWorld()
    due = await world.seed_due("due")
    assert await world.counters.acquire_run("another-run", NOW - timedelta(seconds=30), NOW + timedelta(seconds=30))
    held = dict(guard_docs(world)["sweep"])

    async def no_read(now):
        raise AssertionError("the due query ran under a held guard")

    world.repo.find_due_facts = no_read

    assert await world.service.sweep() == BUSY

    assert state_of(world, due) == "current"
    assert world.daily_docs() == {} and world.line_docs() == {} and world.sink.texts == []
    assert guard_docs(world)["sweep"] == held


async def test_an_overlapping_run_is_refused_while_the_first_one_is_still_working():
    world = JanitorWorld()
    due = await world.seed_due("due")
    second = []

    async def second_run_at_the_ops_post():
        second.append(await world.service.sweep())

    world.sink.at_post = second_run_at_the_ops_post

    first = await world.service.sweep()

    assert second == [BUSY]
    assert first["staled"] == 1 and state_of(world, due) == "stale"
    assert "sweep" not in guard_docs(world)


async def test_the_guard_a_run_takes_lasts_five_minutes_and_names_one_fresh_owner_per_run():
    world = JanitorWorld()
    await world.seed_due("due")
    seen = []

    async def look():
        seen.append(dict(guard_docs(world)["sweep"]))

    world.sink.at_post = look
    await world.service.sweep()
    world.sink.at_post = None
    await world.seed_due("due-2")
    world.sink.at_post = look
    await world.service.sweep()

    assert [doc["run_until"] for doc in seen] == [NOW + timedelta(minutes=5)] * 2
    assert [doc["acquired_at"] for doc in seen] == [NOW] * 2
    assert seen[0]["run_owner"] != seen[1]["run_owner"]


async def test_the_guard_is_freed_after_a_finished_run_so_the_next_run_goes_on():
    world = JanitorWorld()
    await world.seed_due("due")

    first = await world.service.sweep()
    second = await world.service.sweep()

    assert first["staled"] == 1 and "status" not in first
    assert second["due"] == 0 and "status" not in second
    assert guard_docs(world) == {}


async def test_the_guard_is_freed_after_a_failed_run_and_the_error_is_the_runs_own():
    world = JanitorWorld()
    broken = await world.seed_due("broken")
    world.repo.fail_on = {broken}

    with pytest.raises(RuntimeError, match="store unavailable"):
        await world.service.sweep()

    assert guard_docs(world) == {}
    world.repo.fail_on = set()
    assert (await world.service.sweep())["staled"] == 1


async def test_a_failed_release_on_a_failed_run_is_logged_and_the_runs_own_error_wins(caplog):
    world = JanitorWorld()
    broken = await world.seed_due("broken")

    async def failing_release(owner):
        raise ConnectionError("store down")

    world.counters.release_run = failing_release
    world.repo.fail_on = {broken}
    with caplog.at_level(logging.WARNING):
        with pytest.raises(RuntimeError, match="store unavailable"):
            await world.service.sweep()

    assert "the run guard was not released (ConnectionError)" in caplog.text


async def test_a_failed_release_on_a_finished_run_is_logged_and_the_guard_expires_on_its_own(caplog):
    world = JanitorWorld()
    await world.seed_due("due")

    async def failing_release(owner):
        raise ConnectionError("store down")

    world.counters.release_run = failing_release
    with caplog.at_level(logging.WARNING):
        result = await world.service.sweep()
    assert result["staled"] == 1 and "the run guard was not released (ConnectionError)" in caplog.text

    assert await world.service.sweep() == BUSY
    world.now = NOW + timedelta(minutes=5)
    assert (await world.service.sweep()).get("status") != "busy"


async def test_an_expired_guard_is_taken_over():
    world = JanitorWorld()
    due = await world.seed_due("due")
    assert await world.counters.acquire_run("dead-run", NOW - timedelta(minutes=10), NOW - timedelta(minutes=5))

    result = await world.service.sweep()

    assert result["staled"] == 1 and state_of(world, due) == "stale"
    assert guard_docs(world) == {}


async def test_a_guard_that_expires_exactly_now_is_free():
    world = JanitorWorld()
    await world.seed("later", expires_at=FUTURE)
    assert await world.counters.acquire_run("dead-run", NOW - timedelta(minutes=5), NOW)

    assert (await world.service.sweep()).get("status") != "busy"


async def test_a_busy_run_posts_no_failure_alert():
    world = JanitorWorld()
    assert await world.counters.acquire_run("another-run", NOW, NOW + timedelta(minutes=5))

    await world.service.sweep()

    assert world.sink.texts == []

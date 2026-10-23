"""Facts S3 · T10 review L3: every ops post of the janitor is capped, so a hung sink holds no successful run.

The cap is `OPS_POST_TIMEOUT_S` (one constant for the daily line, the breaker alert and the run-failed alert).
Without it the hung post would sleep far past the cap and the sink would still receive its text afterwards.
"""
import asyncio
import logging

import pytest

from tests.support.fact_lifecycle_world import PAST, JanitorWorld

CAP = 0.05


def hung_world(dry_run: bool) -> JanitorWorld:
    world = JanitorWorld(dry_run=dry_run)
    world.service.OPS_POST_TIMEOUT_S = CAP

    async def hang():
        await asyncio.sleep(30)

    world.sink.at_post = hang
    return world


def cut_warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING and "an ops message was cut after" in r.getMessage()]


async def test_a_hung_sink_on_the_daily_line_does_not_hold_a_successful_run(caplog):
    world = hung_world(dry_run=True)
    await world.seed_due("due")

    result = await world.service.sweep()

    assert (result["accounts"], result["would_stale"]) == (1, 1)
    assert world.sink.texts == []
    assert len(cut_warnings(caplog)) == 1
    assert len(world.daily_docs()) == 1 and len(world.line_docs()) == 1       # the snapshot and the day's claim are kept


async def test_a_hung_sink_on_the_breaker_alert_does_not_hold_the_run_or_the_next_account(caplog):
    world = hung_world(dry_run=False)
    await world.seed("already-stale", state="stale", stale_since=PAST)
    held = await world.seed_due("held back by the breaker")
    free = await world.seed_due("other account", account="acc-2")

    result = await world.service.sweep()

    assert (result["breaker_tripped"], result["staled"]) == (1, 1)
    assert world.docs[held]["state"] == "current" and world.docs[free]["state"] == "stale"
    assert world.sink.texts == []
    assert len(cut_warnings(caplog)) == 2                                     # the breaker 🚨 and the daily line


async def test_a_cancellation_during_a_daily_line_post_is_not_swallowed():
    world = JanitorWorld(dry_run=True)
    await world.seed_due("due")
    entered = asyncio.Event()

    async def hang():
        entered.set()
        await asyncio.sleep(30)

    world.sink.at_post = hang
    task = asyncio.ensure_future(world.service.sweep())
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

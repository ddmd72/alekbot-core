"""Facts S4 · T4b review M1 (plan §3.12): an enqueue starts the alert clock `unsuccessful_since` when it is empty, so a
review that is enqueued and never runs still alerts; a success clears it, and a later enqueue starts it again."""
from datetime import timedelta

import pytest

from tests.support.fact_review_world import ReviewWorld
from tests.support.review_rule_builders import NOW

T1 = NOW + timedelta(hours=1)
T2 = NOW + timedelta(hours=2)


@pytest.fixture
def world():
    return ReviewWorld()


async def clock(world):
    state = await world.review_store.get_state("acc")
    assert state is not None
    return state.enqueued_at, state.unsuccessful_since


async def test_the_first_enqueue_of_a_new_account_sets_both(world):
    assert await world.review_store.mark_enqueued("acc", NOW)
    assert await clock(world) == (NOW, NOW)


async def test_the_first_enqueue_of_an_existing_state_starts_the_clock(world):
    await world.seed_state(last_review_at=NOW - timedelta(days=1))
    assert await world.review_store.mark_enqueued("acc", NOW)
    assert await clock(world) == (NOW, NOW)


async def test_the_next_enqueue_keeps_the_earlier_start(world):
    await world.review_store.mark_enqueued("acc", NOW)
    await world.review_store.mark_enqueued("acc", T1)
    assert await clock(world) == (T1, NOW)


async def test_an_enqueue_after_a_failure_keeps_the_failures_start(world):
    await world.seed_state()
    await world.review_store.mark_failed("acc", NOW)
    await world.review_store.mark_enqueued("acc", T1)
    assert await clock(world) == (T1, NOW)


async def test_a_success_clears_the_clock_and_the_next_enqueue_restarts_it(world):
    await world.review_store.mark_enqueued("acc", NOW)
    await world.review_store.mark_succeeded("acc", T1)
    assert await clock(world) == (NOW, None)

    await world.review_store.mark_enqueued("acc", T2)
    assert await clock(world) == (T2, T2)

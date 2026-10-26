"""Facts S4 · T8d2: `mark_undigested` sets `undigested_since` once and keeps the first instant; `mark_digest` clears it.
The real adapter on the review world's evaluating fake."""
from datetime import timedelta

from tests.support.fact_review_world import ReviewWorld
from tests.support.review_rule_builders import NOW

LATER = NOW + timedelta(hours=5)


async def test_it_sets_the_instant_when_none_is_set():
    world = ReviewWorld()
    before = await world.seed_state()

    assert await world.review_store.mark_undigested("acc", NOW) is True

    assert await world.review_store.get_state("acc") == before.model_copy(update={"undigested_since": NOW})


async def test_a_second_mark_keeps_the_first_instant():
    world = ReviewWorld()
    await world.seed_state()
    await world.review_store.mark_undigested("acc", NOW)

    assert await world.review_store.mark_undigested("acc", LATER) is True

    assert (await world.review_store.get_state("acc")).undigested_since == NOW


async def test_a_digest_clears_it_and_the_next_mark_starts_again():
    world = ReviewWorld()
    await world.seed_state(undigested_since=NOW)
    await world.review_store.mark_digest("acc", LATER)

    await world.review_store.mark_undigested("acc", LATER + timedelta(hours=1))

    state = await world.review_store.get_state("acc")
    assert (state.last_digest_at, state.undigested_since) == (LATER, LATER + timedelta(hours=1))


async def test_it_writes_only_that_field_and_creates_no_state():
    world = ReviewWorld()
    assert await world.review_store.mark_undigested("acc", NOW) is False
    assert world.state_doc() is None
    await world.seed_state(offers={"f1": 1}, sent=0)

    await world.review_store.mark_undigested("acc", NOW)

    assert world.state_doc()["offers"] == {"f1": 1} and world.state_doc()["undigested_since"] == NOW

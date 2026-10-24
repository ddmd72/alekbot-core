"""Facts S4 · T7b (R44-4): each cached label set is stored with the digest of the canonical texts it was written for. The
real adapter on the review world's evaluating fake."""
import pytest

from src.domain.fact_review_state import FactReviewState
from tests.support.fact_review_world import ReviewWorld


@pytest.fixture
def world():
    return ReviewWorld()


async def read_state(world, account_id: str = "acc") -> FactReviewState:
    state = await world.review_store.get_state(account_id)
    assert state is not None
    return state


async def test_set_language_stores_the_digest_of_that_language_and_keeps_the_others(world):
    await world.seed_state(labels={"ru": {"still_true": "Да"}, "pl": {"still_true": "Tak"}},
                           label_digests={"ru": "old-ru", "pl": "old-pl"})

    assert await world.review_store.set_language("acc", "ru", {"still_true": "Да!"}, "new-digest")

    state = await read_state(world)
    assert dict(state.label_digests) == {"ru": "new-digest", "pl": "old-pl"}
    assert dict(state.labels["ru"]) == {"still_true": "Да!"} and dict(state.labels["pl"]) == {"still_true": "Tak"}
    assert world.state_doc()["label_digests"] == {"ru": "new-digest", "pl": "old-pl"}


async def test_the_digests_round_trip_through_all_states(world):
    seeded = await world.seed_state(labels={"ru": {"still_true": "Да"}}, label_digests={"ru": "d1"})

    (state,) = await world.review_store.all_states()

    assert state == seeded and dict(state.label_digests) == {"ru": "d1"}


async def test_a_state_stored_before_the_digests_reads_with_none(world):
    await world.seed_state(labels={"ru": {"still_true": "Да"}})
    document = world.state_doc()
    document.pop("label_digests")
    await world.store.db.collection(world.env.fact_review_state_collection).document("acc").set(document)

    assert dict((await read_state(world)).label_digests) == {}


def test_digests_are_kept_by_language_code():
    with pytest.raises(ValueError, match="label digests"):
        FactReviewState(account_id="acc", label_digests={"Russian": "d1"})

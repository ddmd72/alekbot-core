"""Facts S4 · T4b: the review state's marks. Each writes exactly its fields, each replaced whole, validated as the whole
new state; only `mark_enqueued` creates a state document. The real adapter on the review world's evaluating fake."""
from datetime import date, datetime, timedelta

import pytest

from src.adapters.firestore_fact_review_store import state_document
from src.domain.fact_review_state import FactReviewState
from tests.support.fact_review_world import ReviewWorld
from tests.support.review_rule_builders import NOW

T1 = NOW + timedelta(hours=1)
T2 = NOW + timedelta(hours=2)


@pytest.fixture
def world():
    return ReviewWorld()


async def read_state(world, account_id: str = "acc") -> FactReviewState:
    state = await world.review_store.get_state(account_id)
    assert state is not None
    return state


# ------------------------------------------------------------------------------------------------ all_states

async def test_every_state_round_trips_through_all_states(world):
    full = await world.seed_state(
        owner_user_id="u1", enqueued_at=NOW, started_at=NOW, last_review_at=NOW, last_failed_at=NOW,
        unsuccessful_since=NOW, offers={"f1": 2, "f2": 1}, last_digest_at=NOW, undigested_since=NOW,
        oldest_alert_at=NOW, queue_alert_at=NOW, current_question_id="q1", sent_on=date(2026, 10, 24), sent=1,
        delivery_failed_on=date(2026, 10, 23), message_language="uk",
        labels={"uk": {"still_true": "Так", "ended": "Ні"}, "en": {"still_true": "Yes"}},
    )
    other = await world.seed_state(account_id="acc-2")

    states = await world.review_store.all_states()

    assert sorted(states, key=lambda s: s.account_id) == [full, other]
    assert all(getattr(full, name) not in (None, 0, {}) for name in FactReviewState.model_fields)


async def test_all_states_skips_one_that_no_longer_validates(world):
    good = await world.seed_state(account_id="acc-good")
    await world.seed_raw_state("acc-bad", {"account_id": "acc-bad", "sent": -1})

    assert await world.review_store.all_states() == [good]


async def test_all_states_of_an_empty_store_is_empty(world):
    assert await world.review_store.all_states() == []


# ------------------------------------------------------------------------------------------------ creation

async def test_mark_enqueued_creates_a_missing_state(world):
    assert await world.review_store.mark_enqueued("acc", NOW) is True
    assert await read_state(world) == FactReviewState(account_id="acc", enqueued_at=NOW)


@pytest.mark.parametrize("mark", [
    lambda s: s.mark_started("acc", NOW),
    lambda s: s.mark_succeeded("acc", NOW),
    lambda s: s.mark_failed("acc", NOW),
    lambda s: s.set_offers("acc", {"f1": 1}),
    lambda s: s.mark_removed("acc", ["f1"]),
    lambda s: s.mark_digest("acc", NOW),
    lambda s: s.mark_oldest_alert("acc", NOW),
    lambda s: s.mark_queue_alert("acc", NOW),
    lambda s: s.mark_queue_alert("acc", None),
    lambda s: s.set_language("acc", "uk", {"still_true": "Так"}),
])
async def test_every_other_mark_leaves_a_missing_state_missing(world, mark):
    assert await mark(world.review_store) is False
    assert world.state_doc() is None


# ------------------------------------------------------------------------------------------------ each mark

async def test_mark_enqueued_on_an_existing_state_changes_only_enqueued_at(world):
    before = await world.seed_state(enqueued_at=NOW, started_at=NOW, offers={"f1": 1})
    assert await world.review_store.mark_enqueued("acc", T1)
    assert await read_state(world) == before.model_copy(update={"enqueued_at": T1})


async def test_mark_started(world):
    before = await world.seed_state(enqueued_at=NOW)
    assert await world.review_store.mark_started("acc", T1)
    assert await read_state(world) == before.model_copy(update={"started_at": T1})


async def test_mark_succeeded_sets_the_review_time_and_clears_unsuccessful_since(world):
    before = await world.seed_state(unsuccessful_since=NOW, last_failed_at=NOW)
    assert await world.review_store.mark_succeeded("acc", T1)
    assert await read_state(world) == before.model_copy(update={"last_review_at": T1, "unsuccessful_since": None})


async def test_mark_failed_sets_unsuccessful_since_only_when_it_is_empty(world):
    await world.seed_state(last_review_at=NOW)

    assert await world.review_store.mark_failed("acc", T1)
    first = await read_state(world)
    assert (first.last_failed_at, first.unsuccessful_since) == (T1, T1)

    assert await world.review_store.mark_failed("acc", T2)
    second = await read_state(world)
    assert (second.last_failed_at, second.unsuccessful_since) == (T2, T1)

    assert await world.review_store.mark_succeeded("acc", T2)
    assert await world.review_store.mark_failed("acc", T2 + timedelta(hours=1))
    third = await read_state(world)
    assert third.unsuccessful_since == T2 + timedelta(hours=1)


async def test_set_offers_replaces_the_whole_map(world):
    await world.seed_state(offers={"f1": 2, "f2": 1})
    assert await world.review_store.set_offers("acc", {"f2": 3, "f3": 1})
    assert dict((await read_state(world)).offers) == {"f2": 3, "f3": 1}
    assert world.state_doc()["offers"] == {"f2": 3, "f3": 1}


async def test_mark_removed_drops_only_those_facts_offers(world):
    await world.seed_state(offers={"f1": 2, "f2": 1, "f3": 4})
    assert await world.review_store.mark_removed("acc", ["f1", "f3", "not-offered"])
    assert world.state_doc()["offers"] == {"f2": 1}


async def test_mark_digest_sets_the_digest_time_and_clears_undigested_since(world):
    before = await world.seed_state(undigested_since=NOW, last_digest_at=NOW - timedelta(days=8))
    assert await world.review_store.mark_digest("acc", T1)
    assert await read_state(world) == before.model_copy(update={"last_digest_at": T1, "undigested_since": None})


async def test_mark_oldest_alert(world):
    before = await world.seed_state()
    assert await world.review_store.mark_oldest_alert("acc", T1)
    assert await read_state(world) == before.model_copy(update={"oldest_alert_at": T1})


async def test_mark_queue_alert_sets_and_rearms(world):
    before = await world.seed_state()
    assert await world.review_store.mark_queue_alert("acc", T1)
    assert (await read_state(world)).queue_alert_at == T1
    assert await world.review_store.mark_queue_alert("acc", None)
    assert await read_state(world) == before
    assert world.state_doc()["queue_alert_at"] is None


async def test_set_language_replaces_that_languages_labels_and_keeps_the_others(world):
    await world.seed_state(message_language="en", labels={"en": {"still_true": "Yes"}, "uk": {"still_true": "Так",
                                                                                              "stale": "старе"}})

    assert await world.review_store.set_language("acc", "uk", {"still_true": "Так", "ended": "Закінчилось"})

    state = await read_state(world)
    assert state.message_language == "uk"
    assert {lang: dict(texts) for lang, texts in state.labels.items()} == {
        "en": {"still_true": "Yes"}, "uk": {"still_true": "Так", "ended": "Закінчилось"},
    }
    assert world.state_doc()["labels"]["uk"] == {"still_true": "Так", "ended": "Закінчилось"}


async def test_set_language_adds_a_new_language(world):
    await world.seed_state()
    assert await world.review_store.set_language("acc", "es", {"still_true": "Sí"})
    state = await read_state(world)
    assert state.message_language == "es" and dict(state.labels["es"]) == {"still_true": "Sí"}


# ------------------------------------------------------------------------------------------------ exactly its fields

async def test_a_mark_keeps_the_fields_of_other_writers_and_of_the_claim(world):
    seeded = FactReviewState(account_id="acc", owner_user_id="u1", current_question_id="q1",
                             sent_on=date(2026, 10, 24), sent=2, offers={"f1": 1})
    await world.seed_raw_state("acc", {**state_document(seeded), "written_by_someone_else": "kept"})

    assert await world.review_store.mark_started("acc", T1)

    doc = world.state_doc()
    assert doc["written_by_someone_else"] == "kept"
    assert doc == {**state_document(seeded), "written_by_someone_else": "kept", "started_at": T1}


async def test_a_mark_on_a_state_document_with_only_some_fields_writes_only_its_own(world):
    await world.seed_raw_state("acc", {"account_id": "acc", "sent": 0})

    assert await world.review_store.mark_digest("acc", T1)

    assert world.state_doc() == {"account_id": "acc", "sent": 0, "last_digest_at": T1, "undigested_since": None}


# ------------------------------------------------------------------------------------------------ refusals

@pytest.mark.parametrize("mark", [
    lambda s: s.set_offers("acc", {"f1": -1}),
    lambda s: s.set_language("acc", "ukr", {"still_true": "Так"}),
    lambda s: s.set_language("acc", "UK", {"still_true": "Так"}),
    lambda s: s.mark_started("acc", datetime(2026, 10, 24, 9, 0)),
    lambda s: s.mark_queue_alert("acc", datetime(2026, 10, 24, 9, 0)),
])
async def test_a_value_the_state_does_not_validate_is_refused_and_nothing_is_written(world, mark):
    await world.seed_state(offers={"f1": 1})
    before = world.state_doc()
    with pytest.raises(ValueError):
        await mark(world.review_store)
    assert world.state_doc() == before


async def test_a_state_that_no_longer_validates_is_raised_not_overwritten(world):
    await world.seed_raw_state("acc", {"account_id": "acc", "sent": -1})
    with pytest.raises(ValueError):
        await world.review_store.mark_started("acc", T1)
    assert world.state_doc() == {"account_id": "acc", "sent": -1}


@pytest.mark.parametrize("mark", [
    lambda s: s.mark_enqueued("", NOW),
    lambda s: s.mark_failed("", NOW),
    lambda s: s.set_language("", "uk", {}),
])
async def test_an_empty_account_id_is_refused(world, mark):
    with pytest.raises(ValueError):
        await mark(world.review_store)
    assert world.store.db.collection(world.env.fact_review_state_collection).docs == {}

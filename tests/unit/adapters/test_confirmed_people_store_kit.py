"""Confirmed people store kit (fact profile RFC §4.5): `FirestoreConfirmedPeopleRepository` checked against its port
docstring on the evaluating Firestore fake. Examples come from the invented universe."""
import pytest

from src.adapters.firestore_confirmed_people_repo import FirestoreConfirmedPeopleRepository
from src.domain.confirmed_people import parse_confirmed_people
from tests.support.fact_kit import ACC, OTHER
from tests.support.fact_store import make_env_config
from tests.support.firestore_eval import EvalDb

COLLECTION = "eval_confirmed_people"
HOUSEHOLD = parse_confirmed_people({
    "user": {"name": "Alex Doe", "forms": ["Alex", "Bo"]},
    "people": [{"name": "Clara Moreira", "forms": ["Clara"], "relation": "partner"},
               {"name": "Maria Doe", "forms": ["Mariia", "Mia"], "relation": "mother"}]})
SMALLER = parse_confirmed_people({"user": {"name": "Alex Doe", "forms": []}, "people": []})


def env():
    config = make_env_config()
    config.confirmed_people_collection = COLLECTION
    return config


@pytest.fixture
def db():
    return EvalDb()


@pytest.fixture
def lists(db):
    return FirestoreConfirmedPeopleRepository(db, env())


def docs(db):
    return db.collection(COLLECTION).docs


async def test_replace_then_get_round_trips_the_order_and_the_forms(lists):
    await lists.replace(ACC, HOUSEHOLD, "owner_script")
    assert await lists.get(ACC) == HOUSEHOLD


async def test_the_document_id_is_the_account_id_and_carries_account_source_and_time(db, lists):
    await lists.replace(ACC, HOUSEHOLD, "review_answer")

    assert list(docs(db)) == [ACC]
    stored = docs(db)[ACC]
    assert stored["account_id"] == ACC and stored["source"] == "review_answer"
    assert stored["confirmed_at"] is not None
    assert stored["user"] == {"name": "Alex Doe", "forms": ["Alex", "Bo"]}
    assert [p["name"] for p in stored["people"]] == ["Clara Moreira", "Maria Doe"]


async def test_replace_replaces_the_whole_list(lists):
    await lists.replace(ACC, HOUSEHOLD, "owner_script")
    await lists.replace(ACC, SMALLER, "owner_script")
    assert await lists.get(ACC) == SMALLER


async def test_another_accounts_list_is_neither_touched_nor_read(db, lists):
    await lists.replace(OTHER, SMALLER, "owner_script")
    await lists.replace(ACC, HOUSEHOLD, "owner_script")
    await lists.replace(ACC, SMALLER, "owner_script")

    assert await lists.get(OTHER) == SMALLER
    assert sorted(docs(db)) == sorted([ACC, OTHER])


async def test_no_document_is_none(lists):
    assert await lists.get(ACC) is None


async def test_a_stored_document_that_does_not_parse_raises_and_is_not_none(db, lists):
    docs(db)[ACC] = {"account_id": ACC, "user": {"name": ""}, "people": []}
    with pytest.raises(ValueError):
        await lists.get(ACC)


async def test_a_document_stored_under_another_account_raises(db, lists):
    await lists.replace(OTHER, SMALLER, "owner_script")
    docs(db)[ACC] = dict(docs(db)[OTHER])
    with pytest.raises(ValueError):
        await lists.get(ACC)


async def test_an_unknown_source_raises_before_any_write(db, lists):
    with pytest.raises(ValueError):
        await lists.replace(ACC, HOUSEHOLD, "model_guess")
    assert docs(db) == {}

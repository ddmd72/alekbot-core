"""Entry ids on the confirmed people store (email index v2 slice 0): `replace` assigns them inside one transaction on the
evaluating Firestore fake. Invented universe only."""
import pytest

from src.adapters.firestore_confirmed_people_repo import FirestoreConfirmedPeopleRepository
from src.domain.confirmed_people import parse_confirmed_people
from tests.support.fact_kit import ACC, OTHER
from tests.support.fact_store import make_env_config
from tests.support.firestore_eval import EvalDb, eval_transactions

COLLECTION = "eval_confirmed_people_ids"
HOUSEHOLD = parse_confirmed_people({
    "user": {"name": "Alex Doe", "forms": ["Alex", "Bo"]},
    "people": [{"name": "Clara Moreira", "forms": ["Clara"], "relation": "partner"},
               {"name": "Maria Doe", "forms": ["Maria A. Doe", "Mariia", "Mia"], "relation": "mother"}]})
RENAMED = parse_confirmed_people({
    "user": {"name": "Alex Doe", "forms": ["Alex", "Bo"]},
    "people": [{"name": "Maria A. Doe", "forms": ["Maria Doe", "Mia"], "relation": "mother"},
               {"name": "Clara Moreira", "forms": ["Clara"], "relation": "partner"},
               {"name": "Peter Doe", "forms": [], "relation": "father"}]})


@pytest.fixture
def db():
    with eval_transactions():
        yield EvalDb()


@pytest.fixture
def lists(db):
    config = make_env_config()
    config.confirmed_people_collection = COLLECTION
    return FirestoreConfirmedPeopleRepository(db, config)


def docs(db):
    return db.collection(COLLECTION).docs


async def test_the_first_replace_stores_ids_and_the_counter(db, lists):
    await lists.replace(ACC, HOUSEHOLD, "owner_script")
    stored = docs(db)[ACC]
    assert [p["entry_id"] for p in stored["people"]] == [1, 2] and stored["next_entry_id"] == 3
    assert "entry_id" not in stored["user"]


async def test_a_second_replace_with_a_renamed_list_keeps_ids_and_gives_the_new_person_the_counter(db, lists):
    await lists.replace(ACC, HOUSEHOLD, "owner_script")
    await lists.replace(ACC, RENAMED, "owner_script")
    assert {p["name"]: p["entry_id"] for p in docs(db)[ACC]["people"]} == \
        {"Maria A. Doe": 2, "Clara Moreira": 1, "Peter Doe": 3}
    assert docs(db)[ACC]["next_entry_id"] == 4


async def test_get_returns_the_ids(lists):
    await lists.replace(ACC, HOUSEHOLD, "owner_script")
    got = await lists.get(ACC)
    assert [p.entry_id for p in got.people] == [1, 2] and got.next_entry_id == 3


async def test_a_change_inside_the_transaction_is_retried_with_no_duplicate_id(db, lists):
    await lists.replace(ACC, HOUSEHOLD, "owner_script")
    stored = dict(docs(db)[ACC])
    stored["people"] = list(stored["people"]) + [{"name": "Hanna Doe", "forms": [], "relation": "sister", "entry_id": 3}]
    db.interfere_on_txn_read(COLLECTION, ACC, {"people": stored["people"], "next_entry_id": 4}, after_read=True)
    await lists.replace(ACC, RENAMED, "owner_script")
    found = [p["entry_id"] for p in docs(db)[ACC]["people"]]
    assert sorted(found) == [1, 2, 4] and docs(db)[ACC]["next_entry_id"] == 5


async def test_a_stored_list_that_does_not_parse_raises_and_writes_nothing(db, lists):
    broken = {"account_id": ACC, "user": {"name": "Alex Doe", "forms": []}, "people": [], "next_entry_id": 0}
    docs(db)[ACC] = dict(broken)
    with pytest.raises(ValueError):
        await lists.replace(ACC, HOUSEHOLD, "owner_script")
    assert docs(db)[ACC] == broken


async def test_another_accounts_list_is_untouched(db, lists):
    await lists.replace(OTHER, HOUSEHOLD, "owner_script")
    before = dict(docs(db)[OTHER])
    await lists.replace(ACC, RENAMED, "owner_script")
    assert docs(db)[OTHER] == before

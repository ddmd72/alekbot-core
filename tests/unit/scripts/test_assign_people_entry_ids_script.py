"""The owner's one-time migration script for entry ids, on the evaluating Firestore fake. Invented universe only."""
import pytest

from scripts.people.assign_people_entry_ids import run
from src.adapters.firestore_confirmed_people_repo import FirestoreConfirmedPeopleRepository
from tests.support.fact_kit import ACC
from tests.support.fact_store import make_env_config
from tests.support.firestore_eval import EvalDb, eval_transactions

COLLECTION = "eval_confirmed_people_script"
LEGACY = {"account_id": ACC, "user": {"name": "Alex Doe", "forms": ["Alex"]},
          "people": [{"name": "Clara Moreira", "forms": ["Clara"], "relation": "partner"},
                     {"name": "Hanna Doe", "forms": [], "relation": "sister"}],
          "source": "owner_script", "confirmed_at": None}


@pytest.fixture
def db():
    with eval_transactions():
        yield EvalDb()


@pytest.fixture
def repo(db):
    config = make_env_config()
    config.confirmed_people_collection = COLLECTION
    db.collection(COLLECTION).docs[ACC] = dict(LEGACY, people=[dict(p) for p in LEGACY["people"]])
    return FirestoreConfirmedPeopleRepository(db, config)


async def go(repo, apply=False, show_names=False):
    lines = []
    code = await run(repo, ACC, apply, show_names, lines.append)
    return code, "\n".join(lines)


async def test_the_dry_run_writes_nothing(db, repo):
    before = repr(db.collection(COLLECTION).docs[ACC])
    code, out = await go(repo)
    assert code == 0 and "dry run: nothing written" in out and "no re-profile" in out
    assert repr(db.collection(COLLECTION).docs[ACC]) == before


async def test_apply_writes_the_plan_and_a_second_run_has_nothing_to_write(db, repo):
    code, out = await go(repo, apply=True)
    assert code == 0 and "written: 2 people, next_entry_id 3" in out
    assert [p["entry_id"] for p in db.collection(COLLECTION).docs[ACC]["people"]] == [1, 2]
    code, out = await go(repo, apply=True)
    assert code == 0 and "nothing to write" in out


async def test_output_holds_no_name_without_show_names(repo):
    _, out = await go(repo)
    assert "Clara" not in out and "Hanna" not in out and "Moreira" not in out
    _, shown = await go(repo, show_names=True)
    assert "Clara Moreira" in shown


async def test_no_list_is_a_clean_exit(db, repo):
    del db.collection(COLLECTION).docs[ACC]
    code, out = await go(repo)
    assert code == 0 and "nothing to do" in out

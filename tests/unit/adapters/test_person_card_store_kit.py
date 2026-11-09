"""People card store kit (fact profile RFC §7): `FirestorePersonCardRepository` checked against its port docstring on the
evaluating Firestore fake, and `list_profile_people` on `FirestoreFactRepository` and on the dry-run kit's overlay over it.
Examples come from the invented universe."""
import pytest

from scripts.consolidation.s3_dry_run_kit import NoWriteFactRepository
from src.adapters.firestore_person_card_repo import FirestorePersonCardRepository
from src.domain.entities import FactState
from src.domain.person_cards import PersonCard
from tests.support.fact_kit import ACC, OTHER, make_fact
from tests.support.fact_store import INDEXES_FILE, FactStore, make_env_config
from tests.support.firestore_eval import EvalDb

COLLECTION = "eval_person_cards"
MARIA = PersonCard("doe_mari", "Maria Doe", ("Maria Doe", "Mariia"), ("w:maria", "x:doe", "x:mari"), "mother", 11)
CLARA = PersonCard("klar_moreir", "Clara Moreira", ("Clara Moreira",), ("x:klar", "x:moreir"), None, 10)
PEOPLE = [{"name": "Maria Doe", "role": "mother", "name_keys": ["x:mari", "x:doe"], "role_keys": []}]


def env():
    config = make_env_config()
    config.person_cards_collection = COLLECTION
    return config


@pytest.fixture
def db():
    return EvalDb()


@pytest.fixture
def cards(db):
    return FirestorePersonCardRepository(db, env())


def docs(db):
    return db.collection(COLLECTION).docs


async def test_replace_writes_the_cards_under_account_and_key_and_list_returns_them_whole(db, cards):
    await cards.replace_cards(ACC, [MARIA, CLARA])

    assert sorted(docs(db)) == [f"{ACC}:doe_mari", f"{ACC}:klar_moreir"]
    stored = docs(db)[f"{ACC}:doe_mari"]
    assert {k: stored[k] for k in ("account_id", "card_key", "display", "spellings", "name_keys", "relation",
                                   "fact_count")} == {
        "account_id": ACC, "card_key": "doe_mari", "display": "Maria Doe", "spellings": ["Maria Doe", "Mariia"],
        "name_keys": ["w:maria", "x:doe", "x:mari"], "relation": "mother", "fact_count": 11}
    assert stored["built_at"] is not None
    assert sorted(await cards.list_cards(ACC), key=lambda c: c.card_key) == [MARIA, CLARA]


async def test_replace_overwrites_a_card_and_deletes_the_accounts_other_cards_only(db, cards):
    await cards.replace_cards(ACC, [MARIA, CLARA])
    await cards.replace_cards(OTHER, [CLARA])
    grown = PersonCard("doe_mari", "Maria Doe", ("Maria Doe",), ("x:mari",), None, 14)

    await cards.replace_cards(ACC, [grown])

    assert await cards.list_cards(ACC) == [grown]
    assert await cards.list_cards(OTHER) == [CLARA]
    assert sorted(docs(db)) == [f"{ACC}:doe_mari", f"{OTHER}:klar_moreir"]


async def test_replace_with_nothing_deletes_the_accounts_cards(db, cards):
    await cards.replace_cards(ACC, [MARIA])
    await cards.replace_cards(OTHER, [CLARA])
    await cards.replace_cards(ACC, [])
    assert await cards.list_cards(ACC) == [] and len(await cards.list_cards(OTHER)) == 1


async def test_a_large_replacement_goes_in_batches_of_at_most_500(db, cards):
    many = [PersonCard(f"k{i:04d}", f"P{i}", (f"P{i}",), (), None, 10) for i in range(1100)]
    await cards.replace_cards(ACC, many)
    assert len(docs(db)) == 1100
    assert [len(batch) for batch in db.commits] == [500, 500, 100]


async def test_a_document_that_does_not_parse_is_skipped(db, cards):
    await cards.replace_cards(ACC, [MARIA])
    docs(db)[f"{ACC}:broken"] = {"account_id": ACC, "card_key": "broken"}
    assert await cards.list_cards(ACC) == [MARIA]


def test_the_collection_is_the_prefixed_person_cards():
    from src.config.environment import EnvironmentConfig
    assert EnvironmentConfig.person_cards_collection.fget(type("E", (), {"firestore_collection_prefix": "development_"})()) \
        == "development_person_cards"


# ------------------------------------------------------------------------------------------------ list_profile_people

@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


@pytest.fixture(params=["firestore", "kit"])
def repo(request, store):
    return store.repo if request.param == "firestore" else NoWriteFactRepository(store.repo)


async def test_list_profile_people_returns_the_current_facts_people_of_the_account_only(store, repo):
    await store.repo.add_fact(make_fact("mine", people=PEOPLE))
    await store.repo.add_fact(make_fact("stale one", people=PEOPLE, state=FactState.STALE))
    await store.repo.add_fact(make_fact("theirs", people=PEOPLE, account=OTHER))
    await store.repo.add_fact(make_fact("nobody"))

    got = {fact.id: fact for fact in await repo.list_profile_people(ACC)}

    assert sorted(got) == ["f-mine", "f-nobody"]
    assert got["f-mine"].people == PEOPLE and got["f-nobody"].people is None


async def test_list_profile_people_projects_no_vector(store):
    await store.repo.add_fact(make_fact("mine", people=PEOPLE))
    (got,) = await store.repo.list_profile_people(ACC)
    assert got.vector is None and got.tags_vector is None and got.label_vector is None and got.people == PEOPLE

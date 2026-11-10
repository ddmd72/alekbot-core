"""Profile answer store kit (fact profile RFC §4.7): `FirestoreProfileAnswerRepository` checked against its port docstring
on the evaluating Firestore fake. Examples come from the invented universe."""
from datetime import datetime, timezone

import pytest

from src.adapters.firestore_profile_answer_repo import FirestoreProfileAnswerRepository
from src.domain.profile_answers import (
    EMPTY_ANSWERS, DateAnswer, PersonAnswer, PlaceAnswer, date_point_key, person_point_key, place_point_key,
)
from tests.support.fact_kit import ACC, OTHER
from tests.support.fact_store import make_env_config
from tests.support.firestore_eval import EvalDb

COLLECTION = "eval_profile_answers"
AT = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
ROME = PlaceAnswer(key=place_point_key("Rome", ""), city="Rome", country="", resolution="atlas",
                     geonameid="2000", question_id="pq1", answered_at=AT)
ROME_LOCAL = PlaceAnswer(key=place_point_key("Rome", ""), city="Rome", country="", resolution="local",
                           question_id="pq9", answered_at=AT)
DOE = PersonAnswer(key=person_point_key("lin-1", "Doe"), lineage_id="lin-1", mention="Doe", resolution="entry",
                   name="Jamie Doe", question_id="pq2", answered_at=AT)
ACME = DateAnswer(key=date_point_key("lin-2"), lineage_id="lin-2", mention="in 2026", resolution="date",
                  start="2026-10-01", precision="day", question_id="pq3", answered_at=AT)


def env():
    config = make_env_config()
    config.profile_answers_collection = COLLECTION
    return config


@pytest.fixture
def db():
    return EvalDb()


@pytest.fixture
def store(db):
    return FirestoreProfileAnswerRepository(db, env())


def docs(db):
    return db.collection(COLLECTION).docs


async def test_no_document_is_no_answers(store):
    assert await store.get(ACC) == EMPTY_ANSWERS


async def test_recorded_answers_are_read_back_by_kind(store):
    assert await store.record(ACC, ROME) is True
    assert await store.record(ACC, DOE) is True
    assert await store.record(ACC, ACME) is True

    got = await store.get(ACC)

    assert got.places == {ROME.key: ROME}
    assert got.people == {DOE.key: DOE}
    assert got.dates == {ACME.key: ACME}


async def test_the_document_id_is_the_account_and_carries_account_and_time(db, store):
    await store.record(ACC, ROME)

    assert list(docs(db)) == [ACC]
    stored = docs(db)[ACC]
    assert stored["account_id"] == ACC and stored["updated_at"] is not None
    assert [p["key"] for p in stored["places"]] == [ROME.key] and stored["people"] == [] and stored["dates"] == []


async def test_a_second_answer_for_a_key_is_refused_and_the_first_is_kept(db, store):
    await store.record(ACC, ROME)
    before = dict(docs(db)[ACC])

    assert await store.record(ACC, ROME_LOCAL) is False

    assert docs(db)[ACC] == before
    assert (await store.get(ACC)).places[ROME.key].resolution == "atlas"


async def test_another_accounts_answers_are_neither_read_nor_touched(store):
    await store.record(OTHER, ROME)
    assert await store.get(ACC) == EMPTY_ANSWERS
    assert await store.record(ACC, ROME_LOCAL) is True
    assert (await store.get(OTHER)).places[ROME.key].resolution == "atlas"


async def test_a_document_stored_under_another_account_raises(db, store):
    await store.record(OTHER, ROME)
    docs(db)[ACC] = dict(docs(db)[OTHER])
    with pytest.raises(ValueError):
        await store.get(ACC)
    with pytest.raises(ValueError):
        await store.record(ACC, DOE)


async def test_a_document_that_does_not_parse_raises_and_is_not_empty(db, store):
    docs(db)[ACC] = {"account_id": ACC, "places": [{"key": ROME.key, "resolution": "atlas", "answered_at": AT}]}
    with pytest.raises(ValueError):
        await store.get(ACC)
    with pytest.raises(ValueError):
        await store.record(ACC, DOE)

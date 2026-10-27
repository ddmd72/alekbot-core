"""CompanionMemoryRepository kit (P11 · G3-6): the docstring of `src/ports/companion_memory_repository.py` checked
against `FirestoreCompanionMemoryRepository` on the evaluating Firestore fake (H1), with the index refusal ON (the
declared vector index is (session_id, account_id, vector)). Assertions are on the raw stored documents and on what
`find_nearest` returns. Vectors are 2-dimensional on purpose (H1 does not check the declared 768).

`_adapter(db)` is the one place the adapter is constructed.
"""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import ServiceUnavailable
from google.cloud.firestore_v1.vector import Vector

from src.adapters.firestore_companion_memory_repository import FirestoreCompanionMemoryRepository
from src.domain.companion import CompanionRecord
from tests.support.firestore_eval import EvalDb, stored_vector

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
REC = "eval_companion_records"
SESSION = "slack:C1"
VEC = [1.0, 0.0]
CREATED = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def _adapter(db: EvalDb) -> FirestoreCompanionMemoryRepository:
    return FirestoreCompanionMemoryRepository(db, SimpleNamespace(companion_records_collection=REC))


class Store:
    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE, groups={REC: "development_companion_records"})
        self.repo = _adapter(self.db)

    @property
    def docs(self) -> dict:
        return self.db.collection(REC).docs


@pytest.fixture
def store():
    return Store()


def record(rid="r1", session=SESSION, account="a1", vector=VEC, text="Mixes up past and present tense") -> CompanionRecord:
    return CompanionRecord(id=rid, session_id=session, account_id=account, created_by_user_id="u1", text=text,
                           vector=vector, domain="error_pattern", created_at=CREATED)


def raw(store, rid="r1", **over) -> None:
    doc = {"id": rid, "session_id": SESSION, "account_id": "a1", "created_by_user_id": "u1", "text": "Practised the past tense",
           "vector": Vector(VEC), "tags": [], "domain": "error_pattern", "created_at": CREATED}
    for field, value in over.items():
        if value is ...:
            doc.pop(field)
        else:
            doc[field] = value
    store.docs[rid] = doc


async def found(store, vector=VEC, session=SESSION, account="a1", **kw):
    return [r.id for r in await store.repo.find_nearest(session, account, vector, **kw)]


# -------------------------------------------------------------------------------------------- save_batch

async def test_save_batch_writes_each_record_under_its_id_with_a_vector_value_and_returns_the_count(store):
    assert await store.repo.save_batch([record("r1"), record("r2", vector=[0.9, 0.1])]) == 2
    assert sorted(store.docs) == ["r1", "r2"]
    doc = store.docs["r1"]
    assert (doc["id"], doc["session_id"], doc["account_id"], doc["created_by_user_id"], doc["domain"], doc["tags"]) == (
        "r1", SESSION, "a1", "u1", "error_pattern", [])
    assert doc["text"] == "Mixes up past and present tense"
    assert isinstance(doc["vector"], Vector) and stored_vector(doc["vector"]) == VEC


async def test_save_batch_of_nothing_returns_zero_and_writes_nothing(store):
    assert await store.repo.save_batch([]) == 0
    assert store.docs == {} and store.db.commits == []


async def test_save_batch_goes_in_batches_of_500(store):
    assert await store.repo.save_batch([record(f"r{i:03d}") for i in range(501)]) == 501
    assert len(store.docs) == 501
    assert [len(c) for c in store.db.commits] == [500, 1]


async def test_a_record_without_a_vector_is_stored_and_never_found(store):
    assert await store.repo.save_batch([record("r1", vector=None), record("r2")]) == 2
    assert store.docs["r1"]["vector"] is None
    assert await found(store) == ["r2"]


async def test_saving_the_same_ids_again_replaces_the_documents(store):
    await store.repo.save_batch([record("r1", text="first")])
    await store.repo.save_batch([record("r1", text="second")])
    assert list(store.docs) == ["r1"] and store.docs["r1"]["text"] == "second"


# ----------------------------------------------------------------------------------------- find_nearest

async def test_find_nearest_is_scoped_to_the_session_and_the_account(store):
    await store.repo.save_batch([record("mine"), record("other-account", account="a2"),
                                 record("other-session", session="slack:C2")])
    assert await found(store) == ["mine"]
    assert await found(store, account="a2") == ["other-account"]
    assert await found(store, session="slack:C2") == ["other-session"]
    assert await found(store, account="a3") == []


async def test_find_nearest_returns_the_nearest_first_at_most_limit_and_drops_what_is_past_the_floor(store):
    await store.repo.save_batch([record("d", vector=[0.7, 0.7]), record("a", vector=[1.0, 0.0]),
                                 record("c", vector=[0.8, 0.6]), record("b", vector=[0.9, 0.1]),
                                 record("orthogonal", vector=[0.0, 1.0]), record("opposite", vector=[-1.0, 0.0])])
    assert await found(store, limit=10) == ["a", "b", "c", "d"]
    assert await found(store, limit=2) == ["a", "b"]
    assert await found(store, vector=[0.7, 0.7], limit=1) == ["d"]


async def test_find_nearest_returns_the_vector_as_a_plain_list(store):
    await store.repo.save_batch([record("r1", vector=[0.9, 0.1])])
    [got] = await store.repo.find_nearest(SESSION, "a1", VEC)
    assert got == record("r1", vector=[0.9, 0.1]) and type(got.vector) is list


async def test_find_nearest_skips_a_record_that_does_not_validate_and_takes_a_missing_id_from_the_document(store):
    raw(store, "ok")
    raw(store, "no-domain", domain=...)
    raw(store, "fallback-id", id=...)
    got = await store.repo.find_nearest(SESSION, "a1", VEC, limit=10)
    assert sorted(r.id for r in got) == ["fallback-id", "ok"]


async def test_find_nearest_propagates_backend_errors(store):
    await store.repo.save_batch([record("r1")])
    store.db.collection(REC).fail = ServiceUnavailable("down")
    with pytest.raises(ServiceUnavailable):
        await store.repo.find_nearest(SESSION, "a1", VEC)

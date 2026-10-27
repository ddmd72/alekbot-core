"""MemoryFactDraftRepository kit (P11, G3 store 5): the docstring of `src/ports/memory_fact_draft_repository.py` checked
against `FirestoreMemoryFactDraftRepository` on the evaluating Firestore fake (H1), with the index refusal ON (the adapter
issues no query). Assertions are on the raw stored documents and on what `get_draft` returns.

`_adapter(db)` is the one place the adapter is constructed.
"""
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.adapters import firestore_memory_fact_draft_repository
from src.adapters.firestore_memory_fact_draft_repository import FirestoreMemoryFactDraftRepository
from src.domain.memory_fact import MemoryFactDraft, ProposedFact
from tests.support.clock_instants import UTC_MIDNIGHT, pin_datetime
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
COL = "eval_memory_fact_drafts"
NOW = UTC_MIDNIGHT.utc
GYM = {"text": "Alex goes to the Iron Temple gym on Mondays", "source": "gym schedule"}
TEA = {"text": "Maria likes tea", "source": "family"}


def _adapter(db: EvalDb) -> FirestoreMemoryFactDraftRepository:
    return FirestoreMemoryFactDraftRepository(db, SimpleNamespace(memory_fact_drafts_collection=COL))


class Store:
    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE)
        self.repo = _adapter(self.db)

    @property
    def docs(self) -> dict:
        return self.db.collection(COL).docs


@pytest.fixture
def store(monkeypatch):
    pin_datetime(monkeypatch, firestore_memory_fact_draft_repository, NOW)
    return Store()


def draft(*facts, origin="u1:C1") -> MemoryFactDraft:
    return MemoryFactDraft(facts=[ProposedFact(**f) for f in (facts or (GYM,))], origin_session_id=origin)


def raw(store, code="ab12", user="u1", **over) -> None:
    doc = {"user_id": user, "facts": [GYM], "origin_session_id": "u1:C1", "created_at": NOW,
           "expires_at": NOW + timedelta(days=7)}
    for field, value in over.items():
        if value is ...:
            doc.pop(field)
        else:
            doc[field] = value
    store.docs[f"{user}:{code}"] = doc


async def test_create_stores_every_field_and_a_seven_day_expiry(store):
    assert await store.repo.create_draft("u1", "ab12", draft(GYM, TEA)) is True
    assert store.docs == {"u1:ab12": {"user_id": "u1", "facts": [GYM, TEA], "origin_session_id": "u1:C1",
                                      "created_at": NOW, "expires_at": NOW + timedelta(days=7)}}
    got = await store.repo.get_draft("u1", "ab12")
    assert got == draft(GYM, TEA)


async def test_a_second_create_of_one_code_returns_false_and_keeps_the_first_draft(store):
    assert await store.repo.create_draft("u1", "ab12", draft(GYM)) is True
    first = dict(store.docs["u1:ab12"])
    assert await store.repo.create_draft("u1", "ab12", draft(TEA, origin="u1:C2")) is False
    assert store.docs["u1:ab12"] == first
    assert await store.repo.get_draft("u1", "ab12") == draft(GYM)


async def test_the_same_code_of_two_users_is_two_drafts_and_a_draft_is_invisible_to_another_user(store):
    await store.repo.create_draft("u1", "ab12", draft(GYM))
    assert await store.repo.create_draft("u2", "ab12", draft(TEA, origin="u2:C9")) is True
    assert await store.repo.get_draft("u1", "ab12") == draft(GYM)
    assert await store.repo.get_draft("u2", "ab12") == draft(TEA, origin="u2:C9")
    assert await store.repo.get_draft("u3", "ab12") is None
    assert await store.repo.get_draft("u1", "zz99") is None


async def test_an_expired_draft_reads_none_at_the_expiry_instant_and_not_a_second_before(store, monkeypatch):
    await store.repo.create_draft("u1", "ab12", draft())
    pin_datetime(monkeypatch, firestore_memory_fact_draft_repository, NOW + timedelta(days=7) - timedelta(seconds=1))
    assert await store.repo.get_draft("u1", "ab12") is not None
    pin_datetime(monkeypatch, firestore_memory_fact_draft_repository, NOW + timedelta(days=7))
    assert await store.repo.get_draft("u1", "ab12") is None
    assert "u1:ab12" in store.docs                      # reading never deletes: the backend's TTL sweep does


async def test_a_naive_expiry_is_read_as_utc(store):
    raw(store, expires_at=NOW.replace(tzinfo=None) - timedelta(days=1))
    assert store.docs["u1:ab12"]["expires_at"].tzinfo is None       # a raw document: the fake stores it as written
    assert await store.repo.get_draft("u1", "ab12") is None


async def test_a_draft_without_expiry_never_expires_and_one_without_origin_reads_none(store):
    raw(store, expires_at=..., origin_session_id=...)
    got = await store.repo.get_draft("u1", "ab12")
    assert got is not None and got.origin_session_id is None and [f.model_dump() for f in got.facts] == [GYM]


@pytest.mark.parametrize("facts", [[{"text": "line one\nline two", "source": "chat"}], [], None], ids=["newline", "empty", "missing"])
async def test_a_draft_with_invalid_facts_reads_none(store, facts):
    raw(store, facts=facts if facts is not None else ...)
    assert await store.repo.get_draft("u1", "ab12") is None


async def test_delete_removes_the_draft_and_a_missing_one_is_not_an_error(store):
    await store.repo.create_draft("u1", "ab12", draft())
    await store.repo.create_draft("u2", "ab12", draft(TEA))
    await store.repo.delete_draft("u1", "ab12")
    await store.repo.delete_draft("u1", "ab12")
    await store.repo.delete_draft("u1", "never-created")
    assert list(store.docs) == ["u2:ab12"]
    assert await store.repo.get_draft("u1", "ab12") is None


async def test_a_draft_can_be_created_again_once_deleted(store):
    await store.repo.create_draft("u1", "ab12", draft(GYM))
    await store.repo.delete_draft("u1", "ab12")
    assert await store.repo.create_draft("u1", "ab12", draft(TEA)) is True
    assert await store.repo.get_draft("u1", "ab12") == draft(TEA)

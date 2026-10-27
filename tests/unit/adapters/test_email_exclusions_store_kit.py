"""EmailExclusionsPort kit (P11 · G3-7): the docstring of `src/ports/email_exclusions_port.py` checked against
`FirestoreEmailExclusionsAdapter` on the evaluating Firestore fake (H1), with the index refusal ON. Only `get_exclusions`
has a caller in `src/` (nothing writes the collection); the other methods are marked "no caller" and tested for what the
docstring promises. The foreign-user delete is not pinned (docstring only).

`_adapter(db)` is the one place the adapter is constructed.
"""
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import ServiceUnavailable

from src.adapters.firestore_email_exclusions_adapter import FirestoreEmailExclusionsAdapter
from src.domain.email import EmailExclusion
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
EXCL = "eval_email_exclusions"
CREATED = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)


def _adapter(db: EvalDb) -> FirestoreEmailExclusionsAdapter:
    return FirestoreEmailExclusionsAdapter(db, SimpleNamespace(email_exclusions_collection=EXCL))


class Store:
    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE, groups={EXCL: "development_email_exclusions"})
        self.adapter = _adapter(self.db)

    @property
    def docs(self) -> dict:
        return self.db.collection(EXCL).docs


@pytest.fixture
def store():
    return Store()


def exclusion(user="u1", kind="sender_domain", pattern="example.com", reason="recurring newsletter") -> EmailExclusion:
    return EmailExclusion(user_id=user, pattern_type=kind, pattern=pattern, reason=reason, created_at=CREATED)


def doc_id(user, kind, pattern) -> str:
    return hashlib.sha256(f"{user}|{kind}|{pattern}".encode()).hexdigest()[:32]


async def test_get_exclusions_returns_only_the_users_patterns_with_their_document_ids(store):
    await store.adapter.add_exclusions([exclusion("u1", "sender_domain", "example.com"),
                                        exclusion("u1", "sender_email", "news@example.org"),
                                        exclusion("u2", "sender_domain", "example.net")])
    got = await store.adapter.get_exclusions("u1")
    assert sorted((e.pattern_type, e.pattern, e.reason, e.created_at, e.exclusion_id) for e in got) == [
        ("sender_domain", "example.com", "recurring newsletter", CREATED, doc_id("u1", "sender_domain", "example.com")),
        ("sender_email", "news@example.org", "recurring newsletter", CREATED, doc_id("u1", "sender_email", "news@example.org"))]
    assert [e.pattern for e in await store.adapter.get_exclusions("u2")] == ["example.net"]
    assert await store.adapter.get_exclusions("u3") == []


async def test_add_exclusions_stores_one_document_per_user_type_and_pattern_and_a_repeat_replaces_it(store):
    await store.adapter.add_exclusions([exclusion(reason="first")])
    await store.adapter.add_exclusions([exclusion(reason="second"), exclusion(kind="sender_email", pattern="example.com")])
    assert sorted(store.docs) == sorted([doc_id("u1", "sender_domain", "example.com"), doc_id("u1", "sender_email", "example.com")])
    stored = store.docs[doc_id("u1", "sender_domain", "example.com")]
    assert stored == {"user_id": "u1", "pattern_type": "sender_domain", "pattern": "example.com", "reason": "second",
                      "created_at": CREATED}
    assert "exclusion_id" not in stored


async def test_add_exclusions_of_nothing_is_a_no_op(store):
    await store.adapter.add_exclusions([])
    assert store.docs == {} and store.db.commits == []


async def test_a_document_missing_a_field_is_skipped_and_the_rest_are_returned(store):
    await store.adapter.add_exclusions([exclusion(pattern="example.com")])
    store.docs["legacy"] = {"user_id": "u1", "pattern_type": "sender_domain", "pattern": "example.org", "created_at": CREATED}
    assert [e.pattern for e in await store.adapter.get_exclusions("u1")] == ["example.com"]


async def test_list_exclusions_answers_what_get_exclusions_answers(store):
    await store.adapter.add_exclusions([exclusion(pattern="example.com"), exclusion(pattern="example.org"), exclusion("u2")])
    assert await store.adapter.list_exclusions("u1") == await store.adapter.get_exclusions("u1")
    assert len(await store.adapter.list_exclusions("u1")) == 2


async def test_delete_exclusion_removes_the_document_by_id_and_a_missing_id_is_a_no_op(store):
    await store.adapter.add_exclusions([exclusion(pattern="example.com"), exclusion(pattern="example.org")])
    gone = doc_id("u1", "sender_domain", "example.com")
    await store.adapter.delete_exclusion("u1", gone)
    await store.adapter.delete_exclusion("u1", gone)
    await store.adapter.delete_exclusion("u1", "never-stored")
    assert [e.pattern for e in await store.adapter.get_exclusions("u1")] == ["example.org"]


async def test_backend_errors_propagate(store):
    await store.adapter.add_exclusions([exclusion()])
    store.db.collection(EXCL).fail = ServiceUnavailable("down")
    with pytest.raises(ServiceUnavailable):
        await store.adapter.get_exclusions("u1")

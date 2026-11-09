"""FirestoreTokenRepository.save / delete: the timestamps written and the document actually removed.

The repository calls `doc_ref.set(...)` and `doc_ref.delete()` without `await` (the profile repository awaits them).
`_SyncWriteDb` therefore models a client whose writes are plain calls and whose reads are awaited, so the write lands
and the stored document can be inspected. The strict-xfail test shows what the real async client (`EvalDb`) does.
"""
import pytest
from google.cloud import firestore

from src.adapters.prompt_v3.firestore_token_repository import FirestoreTokenRepository
from src.domain.prompt_v3.token import Token, TokenCategory, TokenClass, TokenId
from tests.support.firestore_eval import EvalDb
from tests.unit.adapters.prompt_v3.test_firestore_repositories import MockSecurityPort


class _Snap:
    def __init__(self, exists):
        self.exists = exists


class _Ref:
    def __init__(self, store, doc_id):
        self.store, self.doc_id = store, doc_id

    async def get(self):
        return _Snap(self.doc_id in self.store)

    def set(self, data):
        self.store[self.doc_id] = data

    def delete(self):
        self.store.pop(self.doc_id, None)


class _SyncWriteDb:
    def __init__(self):
        self.cols = {}

    def collection(self, name):
        store = self.cols.setdefault(name, {})
        return type("Col", (), {"document": lambda _s, doc_id: _Ref(store, doc_id)})()


def _repo(db):
    return FirestoreTokenRepository(db, "sys_tokens", "usr_tokens", MockSecurityPort())


def _token():
    return Token(id=TokenId("TOK_A"), category=TokenCategory("cat_a"), class_=TokenClass("properties"),
                 content="alpha content", metadata={})


async def test_save_writes_both_server_timestamps():
    db = _SyncWriteDb()
    await _repo(db).save(_token(), "system")
    doc = db.cols["sys_tokens"]["TOK_A"]
    assert doc["created_at"] is firestore.SERVER_TIMESTAMP
    assert doc["updated_at"] is firestore.SERVER_TIMESTAMP


async def test_delete_removes_document_from_system_collection():
    db = _SyncWriteDb()
    repo = _repo(db)
    await repo.save(_token(), "system")
    await repo.delete(TokenId("TOK_A"))
    assert "TOK_A" not in db.cols["sys_tokens"]


async def test_delete_removes_document_from_user_collection():
    db = _SyncWriteDb()
    repo = _repo(db)
    await repo.save(_token(), "user")
    await repo.delete(TokenId("TOK_A"))
    assert "TOK_A" not in db.cols["usr_tokens"]
    assert not db.cols.get("sys_tokens")


@pytest.mark.xfail(strict=True, reason="MUT-D1: save/delete do not await the async client's set()/delete()")
async def test_save_then_delete_on_async_client_fake():
    db = EvalDb()
    repo = _repo(db)
    await repo.save(_token(), "system")
    snap = await db.collection("sys_tokens").document("TOK_A").get()
    assert snap.exists

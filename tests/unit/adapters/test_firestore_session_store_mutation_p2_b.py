"""Mutation survivors of `firestore_session_store` (p2-b): failure logging of `append_message` / `_delete_session`
and the filter + delete of `cleanup_expired_sessions`, driven over EvalDb."""
import time

import pytest

import src.adapters.firestore_session_store as session_module
from tests.support.firestore_eval import EvalDocRef
from tests.unit.adapters.test_session_store_kit import SESS, docs, make_store, msg, raw


class ErrorLog:
    def __init__(self):
        self.lines = []

    def error(self, text, *a, **kw):
        self.lines.append(str(text))


@pytest.fixture
def errors(monkeypatch):
    log = ErrorLog()
    monkeypatch.setattr(session_module.logger, "error", log.error)
    return log


async def test_append_message_failure_is_logged_not_raised(errors, monkeypatch):
    store = make_store()

    async def boom(*a, **kw):
        raise RuntimeError("batch-down")

    monkeypatch.setattr(store, "append_messages_batch", boom)

    await store.append_message("sess-abcdefgh", msg("hi"), owner_id="user-1")

    assert any("Error appending message" in line and "batch-down" in line for line in errors.lines)


async def test_delete_session_failure_is_logged_not_raised(errors, monkeypatch):
    store = make_store()

    async def boom(self, option=None):
        raise RuntimeError("delete-down")

    monkeypatch.setattr(EvalDocRef, "delete", boom)

    await store.delete_session("sess-abcdefgh")

    assert any("Error deleting session" in line and "delete-down" in line for line in errors.lines)


async def test_cleanup_deletes_only_expired_sessions():
    store = make_store()
    store.ttl_hours = 1
    now = time.time()
    col = store.db.collection(SESS)
    await col.document("old-1").set({"history": [raw("a")], "last_activity": now - 7200})
    await col.document("old-2").set({"history": [raw("b")], "last_activity": now - 10 * 3600})
    await col.document("fresh").set({"history": [raw("c")], "last_activity": now - 60})

    deleted = await store.cleanup_expired_sessions()

    assert deleted == 2
    assert set(docs(store)) == {"fresh"}

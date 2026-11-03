"""Mutation survivors of FirestoreSessionStore: the fields `save_session` writes and the owner `append_message` forwards.

Assertions are on the raw stored document over the evaluating Firestore fake.
"""
import datetime

from src.adapters.firestore_session_store import FirestoreSessionStore
from src.domain.llm import Message, MessagePart
from src.domain.session import SessionState
from tests.support.firestore_eval import EvalDb

SESS = "eval_sessions"
SID = "user-1:C1"


def _store() -> FirestoreSessionStore:
    return FirestoreSessionStore(EvalDb(), SESS, max_history_length=10, batch_size=5)


def _doc(store, sid=SID):
    return store.db.collection(SESS).docs[sid]


async def test_save_session_writes_all_bookkeeping_fields():
    store = _store()
    state = SessionState(
        session_id=SID,
        history=[Message(role="user", parts=[MessagePart(text="Hello")])],
        created_at=1000.0,
        last_activity=2000.0,
        owner_id="user-1",
    )

    await store.save_session(SID, state)

    doc = _doc(store)
    assert doc["created_at"] == 1000.0
    assert doc["last_activity"] == 2000.0
    assert doc["owner_id"] == "user-1"
    assert "updated_at" in doc
    assert doc["expires_at"] is not None
    expires = doc["expires_at"]
    now = datetime.datetime.now(datetime.UTC)
    assert now < expires <= now + datetime.timedelta(hours=store.ttl_hours, seconds=5)


async def test_append_message_forwards_owner_id():
    store = _store()

    await store.append_message(SID, Message(role="user", parts=[MessagePart(text="Hi")]), owner_id="owner-9")

    assert _doc(store)["owner_id"] == "owner-9"

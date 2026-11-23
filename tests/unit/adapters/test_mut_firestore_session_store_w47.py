"""Mutation kill for `FirestoreSessionStore.remove_head`: the cut refreshes the session document's `updated_at`."""
from datetime import datetime, timezone

from src.adapters.firestore_session_store import FirestoreSessionStore
from src.domain.llm import Message, MessagePart
from tests.support.firestore_eval import EvalDb

SESS = "eval_sessions"
SID = "usr-1:C1"


async def test_remove_head_refreshes_updated_at_of_the_session_document():
    db = EvalDb()
    store = FirestoreSessionStore(db, SESS, max_history_length=10, batch_size=2)
    for text in ("first", "second", "third"):
        await store.append_messages_batch(SID, [Message(role="user", parts=[MessagePart(text=text)])], owner_id="usr-1")
    before = datetime(2020, 1, 1, tzinfo=timezone.utc)
    db.collection(SESS).docs[SID]["updated_at"] = before   # an old stamp: the cut must replace it, no clock race
    head = (await store.load_session(SID)).history[:2]

    assert await store.remove_head(SID, head) is True

    doc = db.collection(SESS).docs[SID]
    assert [m["parts"][0].get("text") for m in doc["history"]] == ["third"]
    assert doc["updated_at"] > before

"""Owner decision O2, the cut that fails after a successful hand-off: `FirestoreSessionStore` on the evaluating Firestore
fake (H1). The batch was queued, but `remove_head` raised, so the messages stay in the session; the next append over the
window hands the same messages off again (the same `handoff_key`, so the queue record is replaced, not added) and cuts
them. Assertions are on the stored history and on what the overflow callback was given.
"""
import asyncio

import pytest
from google.api_core.exceptions import ServiceUnavailable

from src.adapters.firestore_session_store import FirestoreSessionStore
from src.domain.consolidation import handoff_key
from src.domain.llm import Message, MessagePart
from tests.support.firestore_eval import EvalDb

SESS = "eval_sessions"
SID = "u1:C1"
TEXTS = ["Where is the spare key?", "In the blue drawer.", "Water the ferns on Friday.", "Noted: ferns, Friday.",
         "Book the bike service.", "Booked for Tuesday."]


class CutFailsOnce:
    """The overflow callback: records each hand-off; on its first call it queues one error for the next transactional
    read of the session, which is the cut that follows the hand-off."""

    def __init__(self, db: EvalDb) -> None:
        self.db = db
        self.calls = []

    async def __call__(self, owner, session_id, messages):
        if not self.calls:
            self.db.raise_on_txn_read(SESS, SID, ServiceUnavailable("503 UNAVAILABLE the cut's read failed"))
        self.calls.append(list(messages))


def texts(db):
    return [m["parts"][0].get("text") for m in db.collection(SESS).docs[SID]["history"]]


async def settle(store):
    await asyncio.gather(*list(store._pending_tasks), return_exceptions=True)


@pytest.mark.h1_only("queues a read error with raise_on_txn_read")
async def test_a_failed_cut_keeps_the_handed_off_batch_and_the_next_append_hands_it_off_again_and_cuts_it():
    db = EvalDb()
    handoffs = CutFailsOnce(db)
    store = FirestoreSessionStore(db, SESS, max_history_length=4, batch_size=2, overflow_callback=handoffs)
    for text in TEXTS[:5]:
        await store.append_messages_batch(SID, [Message(role="user", parts=[MessagePart(text=text)])], owner_id="u1")
    await settle(store)
    assert len(handoffs.calls) == 1 and [m.parts[0].text for m in handoffs.calls[0]] == TEXTS[:2]
    assert texts(db) == TEXTS[:5]                     # handed off, but the cut failed: the batch is still stored

    await store.append_messages_batch(SID, [Message(role="model", parts=[MessagePart(text=TEXTS[5])])], owner_id="u1")
    await settle(store)
    assert len(handoffs.calls) == 2 and [m.parts[0].text for m in handoffs.calls[1]] == TEXTS[:2]
    assert handoff_key(SID, handoffs.calls[1]) == handoff_key(SID, handoffs.calls[0])   # the same queue record
    assert texts(db) == TEXTS[2:6]


@pytest.mark.h1_only("queues a read error with raise_on_txn_read")
async def test_remove_head_re_raises_a_backend_error_and_cuts_nothing():
    db = EvalDb()
    store = FirestoreSessionStore(db, SESS, max_history_length=4, batch_size=2)
    for text in TEXTS[:3]:
        await store.append_messages_batch(SID, [Message(role="user", parts=[MessagePart(text=text)])], owner_id="u1")
    head = (await store.load_session(SID)).history[:2]
    db.raise_on_txn_read(SESS, SID, ServiceUnavailable("503 UNAVAILABLE firestore down"))
    with pytest.raises(ServiceUnavailable):
        await store.remove_head(SID, head)
    assert texts(db) == TEXTS[:3]


SERVICE_TEXTS = ["Is the car service still on 2026-11-05?", "Yes, bring the logbook.",
                 "Bring the logbook to the car service.", "Noted: the logbook.",
                 "Is the gym membership valid until November?", "Yes, until Nov 2026."]


class Window:
    """The threshold resolver whose (window, batch) the test changes between appends, as a Cabinet edit does."""

    def __init__(self, values) -> None:
        self.values = values

    async def __call__(self, session_id):
        return self.values


@pytest.mark.h1_only("queues a read error with raise_on_txn_read")
@pytest.mark.parametrize("resized, handed_off, kept", [
    ((3, 3), [SERVICE_TEXTS[:2], SERVICE_TEXTS[2:5]], SERVICE_TEXTS[5:6]),     # smaller: the batch, then new ones of 3
    ((6, 3), [SERVICE_TEXTS[:2]], SERVICE_TEXTS[2:6]),                         # larger: the batch is still handed off
])
async def test_a_batch_whose_cut_failed_keeps_its_boundaries_when_the_window_changes(resized, handed_off, kept):
    db = EvalDb()
    handoffs = CutFailsOnce(db)
    window = Window((4, 2))
    store = FirestoreSessionStore(db, SESS, overflow_callback=handoffs, threshold_resolver=window)
    for text in SERVICE_TEXTS[:5]:
        await store.append_messages_batch(SID, [Message(role="user", parts=[MessagePart(text=text)])], owner_id="u1")
    await settle(store)
    assert [[m.parts[0].text for m in call] for call in handoffs.calls] == [SERVICE_TEXTS[:2]]
    assert texts(db) == SERVICE_TEXTS[:5]               # handed off, but the cut failed

    window.values = resized
    await store.append_messages_batch(SID, [Message(role="model", parts=[MessagePart(text=SERVICE_TEXTS[5])])],
                                      owner_id="u1")
    await settle(store)
    # a repeat of the same messages carries the same key, so the queue keeps its first record: count it once
    batches = {handoff_key(SID, call): [m.parts[0].text for m in call] for call in handoffs.calls}
    assert list(batches.values()) == handed_off
    assert texts(db) == kept

"""SessionStore kit (P11 · G2): the docstring of `src/ports/session_store.py` checked against `FirestoreSessionStore` on
the evaluating Firestore fake (H1), with the index refusal ON. Assertions are on the raw stored documents and on what
`load_session` returns, never on calls.

The two defects of this store are pinned through their real callers, in caller-named files (arbiter rule 2):
P11-G2-3 (a cut batch lost when its hand-off fails) in `tests/unit/services/test_overflow_routing_service_store_pins.py`,
P11-G2-5 (the chat-turn history retry never runs) in `tests/unit/handlers/test_conversation_handler_store_pins.py` and
`tests/unit/services/test_long_turn_service_store_pins.py`.
"""
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import ServiceUnavailable

import src.adapters.firestore_session_store as session_module
from src.adapters.firestore_session_store import FirestoreSessionStore
from src.domain.llm import Message, MessagePart, ToolCall
from src.domain.session import SessionState
from tests.support.firestore_eval import EvalDb, EvalDocRef

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
SESS = "eval_sessions"
SID, SID2 = "u1:C1", "u1:C2"
DAY = 86400.0
TEXTS = ["When is the car service?", "On 2026-11-05.", "Remind me to call dad.", "Done: call dad tomorrow.",
         "Book Clara's weekend in Coimbra.", "Booked: Coimbra, Mar 6-8.", "What is Jamie's school called?",
         "Example School."]


class Handoffs:
    """The overflow callback: records each hand-off as (owner, session id, texts)."""

    def __init__(self) -> None:
        self.calls = []

    async def __call__(self, owner, session_id, messages):
        self.calls.append((owner, session_id, [m.parts[0].text for m in messages]))


def make_store(db=None, handoffs=None, **kw):
    db = db or EvalDb(indexes=INDEXES_FILE, groups={SESS: "development_sessions"})
    return FirestoreSessionStore(db, SESS, max_history_length=kw.pop("max_history_length", 4),
                                 batch_size=kw.pop("batch_size", 2), overflow_callback=handoffs, **kw)


@pytest.fixture
def handoffs():
    return Handoffs()


@pytest.fixture
def store(handoffs):
    return make_store(handoffs=handoffs)


def docs(store):
    return store.db.collection(SESS).docs


def texts(store, sid=SID):
    return [m["parts"][0].get("text") for m in docs(store)[sid]["history"]]


def msg(text, role="user"):
    return Message(role=role, parts=[MessagePart(text=text)])


def raw(text, role="user", created_at=1.0):
    return {"role": role, "parts": [{"text": text}], "created_at": created_at}


async def settle(store):
    await asyncio.gather(*list(store._pending_tasks), return_exceptions=True)


async def append_each(store, n, sid=SID, owner="u1"):
    for text in TEXTS[:n]:
        await store.append_messages_batch(sid, [msg(text)], owner_id=owner)


# --------------------------------------------------------------------------------------------------- order, shape

async def test_appends_keep_append_order_across_calls(store):
    await store.append_messages_batch(SID, [msg(TEXTS[0])], owner_id="u1")
    await store.append_messages_batch(SID, [msg(TEXTS[1], "model"), msg(TEXTS[2])], owner_id="u1")
    assert texts(store) == TEXTS[:3]
    loaded = await store.load_session(SID)
    assert [(m.role, m.parts[0].text) for m in loaded.history] == [("user", TEXTS[0]), ("model", TEXTS[1]),
                                                                   ("user", TEXTS[2])]


async def test_first_append_creates_the_document_with_owner_and_activity(store):
    before = time.time()
    await store.append_messages_batch(SID, [msg(TEXTS[0])], owner_id="u1")
    doc = docs(store)[SID]
    assert doc["owner_id"] == "u1"
    assert doc["created_at"] == doc["last_activity"] and doc["last_activity"] >= before
    assert {"updated_at", "expires_at"} <= set(doc)


async def test_owner_defaults_to_the_session_id_and_a_stored_owner_is_kept(store):
    await store.append_messages_batch(SID2, [msg(TEXTS[0])])
    assert docs(store)[SID2]["owner_id"] == SID2
    await store.append_messages_batch(SID, [msg(TEXTS[0])], owner_id="u1")
    await store.append_messages_batch(SID, [msg(TEXTS[1])], owner_id="u2")
    assert docs(store)[SID]["owner_id"] == "u1"
    assert (await store.load_session(SID)).owner_id == "u1"


async def test_save_session_writes_the_state_owner_or_the_session_id(store):
    await store.save_session(SID, SessionState(session_id=SID, history=[msg(TEXTS[0])]))
    assert docs(store)[SID]["owner_id"] == SID
    await store.save_session(SID, SessionState(session_id=SID, history=[msg(TEXTS[0])], owner_id="u2"))
    assert docs(store)[SID]["owner_id"] == "u2"


async def test_the_collection_name_is_kept_or_completed():
    db = EvalDb()
    await FirestoreSessionStore(db).append_messages_batch(SID, [msg(TEXTS[0])])
    await FirestoreSessionStore(db, "eval_").append_messages_batch(SID, [msg(TEXTS[1])])
    assert {name: list(col.docs) for name, col in db.cols.items()} == {"sessions": [SID], "eval_sessions": [SID]}


async def test_two_sessions_of_one_owner_are_independent(store):
    await store.append_messages_batch(SID, [msg(TEXTS[0])], owner_id="u1")
    await store.append_messages_batch(SID2, [msg(TEXTS[1])], owner_id="u1")
    assert texts(store, SID) == [TEXTS[0]] and texts(store, SID2) == [TEXTS[1]]


# --------------------------------------------------------------------------------------------------- the window

async def test_overflow_cuts_the_oldest_batch_and_hands_it_off_oldest_first(store, handoffs):
    await append_each(store, 5)
    await settle(store)
    assert texts(store) == TEXTS[2:5]
    assert handoffs.calls == [("u1", SID, TEXTS[:2])]


async def test_a_long_append_cuts_several_batches_in_one_call(store, handoffs):
    await append_each(store, 1)
    await store.append_messages_batch(SID, [msg(t) for t in TEXTS[1:8]], owner_id="u1")
    await settle(store)
    assert texts(store) == TEXTS[4:8]
    assert handoffs.calls == [("u1", SID, TEXTS[:2]), ("u1", SID, TEXTS[2:4])]


async def test_threshold_resolver_override_and_none_fallback(handoffs):
    async def resolve(session_id):
        return (3, 1) if session_id == SID else None

    store = make_store(handoffs=handoffs, threshold_resolver=resolve)
    await append_each(store, 4, SID)
    await append_each(store, 4, SID2)
    await settle(store)
    assert texts(store, SID) == TEXTS[1:4]          # window 3, batch 1
    assert texts(store, SID2) == TEXTS[:4]          # the store's window 4: not over it
    assert handoffs.calls == [("u1", SID, TEXTS[:1])]


# --------------------------------------------------------------------------------------------------- atomicity

async def test_concurrent_appends_are_all_kept(store):
    await store.append_messages_batch(SID, [msg(TEXTS[0])], owner_id="u1")
    stored = docs(store)[SID]["history"]
    # another append commits between this append's read and its commit
    store.db.interfere_on_txn_read(SESS, SID, {"history": stored + [raw(TEXTS[1])], "last_activity": 1.0},
                                   after_read=True)
    await store.append_messages_batch(SID, [msg(TEXTS[2])], owner_id="u1")
    assert texts(store) == TEXTS[:3]


async def test_a_repeated_append_appends_twice(store):
    m = msg(TEXTS[0])
    await store.append_messages_batch(SID, [m], owner_id="u1")
    await store.append_messages_batch(SID, [m], owner_id="u1")
    assert texts(store) == [TEXTS[0], TEXTS[0]]


async def test_save_session_replaces_history_whole(store):
    await append_each(store, 3)
    await store.save_session(SID, SessionState(session_id=SID, history=[msg(TEXTS[5])], owner_id="u1"))
    assert texts(store) == [TEXTS[5]]


# --------------------------------------------------------------------------------------------------- projection, legacy

async def test_round_trip_keeps_text_full_text_file_data_tool_call_tool_response(store):
    part = MessagePart(text=TEXTS[3], full_text="Done: call dad tomorrow at 10:00.", consolidation_text=TEXTS[2],
                       file_data={"uri": "gs://bucket/u1/birth-certificate.pdf", "mime_type": "application/pdf"},
                       tool_call=ToolCall(name="manage_self_reminders", args={"text": "call dad"},
                                          thought_signature="sig-1"),
                       tool_response={"name": "manage_self_reminders", "response": {"ok": True}})
    sent = Message(role="model", parts=[part], raw_content={"x": 1}, created_at=1000.5)
    await store.append_messages_batch(SID, [sent], owner_id="u1")
    got = (await store.load_session(SID)).history[0]
    assert got.role == "model" and got.created_at == 1000.5 and got.raw_content is None
    expected = part.model_copy(update={"tool_call": ToolCall(name="manage_self_reminders", args={"text": "call dad"})})
    assert got.parts == [expected]                  # thought_signature and raw_content are not stored


async def test_messages_without_parts_or_content_round_trip(store):
    sent = [Message(role="user", parts=[], created_at=1.0), Message(role="model", parts=[MessagePart()], created_at=2.0)]
    await store.append_messages_batch(SID, sent, owner_id="u1")
    assert docs(store)[SID]["history"][1]["parts"] == [{}]
    assert (await store.load_session(SID)).history == sent


async def test_append_message_appends_one_and_swallows_errors(store):
    await store.append_message(SID, msg(TEXTS[0]), owner_id="u1")
    store.db.raise_on_txn_read(SESS, SID, ServiceUnavailable("503 UNAVAILABLE firestore down"))
    assert await store.append_message(SID, msg(TEXTS[1]), owner_id="u1") is None
    assert texts(store) == TEXTS[:1]


async def test_legacy_message_without_created_at_or_role(store, monkeypatch):
    docs(store)[SID] = {"history": [{"parts": [{"text": TEXTS[0]}]}], "owner_id": "u1", "created_at": 1.0,
                        "last_activity": time.time()}
    now = time.time()
    monkeypatch.setattr(session_module, "time", SimpleNamespace(time=lambda: now))
    got = (await store.load_session(SID)).history
    assert [(m.role, m.created_at, m.parts[0].text) for m in got] == [("user", now, TEXTS[0])]


async def test_legacy_unknown_part_keys_are_ignored(store):
    docs(store)[SID] = {"history": [{"role": "user", "parts": [{"text": TEXTS[0], "thought": "x", "kind": 3}],
                                     "created_at": 5.0}], "owner_id": "u1", "created_at": 1.0,
                        "last_activity": time.time()}
    got = (await store.load_session(SID)).history
    assert got == [Message(role="user", parts=[MessagePart(text=TEXTS[0])], created_at=5.0)]


# --------------------------------------------------------------------------------------------------- expiry, errors

async def test_a_session_idle_over_90_days_is_deleted_and_read_empty(store):
    docs(store)[SID] = {"history": [raw(TEXTS[0])], "owner_id": "u1", "created_at": 1.0,
                        "last_activity": time.time() - 91 * DAY}
    got = await store.load_session(SID)
    assert (got.session_id, got.history) == (SID, [])
    assert SID not in docs(store)


async def test_a_session_idle_under_90_days_is_kept(store):
    docs(store)[SID] = {"history": [raw(TEXTS[0])], "owner_id": "u1", "created_at": 1.0,
                        "last_activity": time.time() - 89 * DAY}
    assert [m.parts[0].text for m in (await store.load_session(SID)).history] == [TEXTS[0]]


async def test_an_append_to_an_idle_session_keeps_its_old_history(store):
    docs(store)[SID] = {"history": [raw(TEXTS[0])], "owner_id": "u1", "created_at": 1.0,
                        "last_activity": time.time() - 91 * DAY}
    await store.append_messages_batch(SID, [msg(TEXTS[1])], owner_id="u1")
    assert texts(store) == TEXTS[:2]


async def test_unknown_and_empty_ids_read_empty(store):
    assert (await store.load_session("u9:C9")).history == []
    empty = await store.load_session("")
    assert (empty.session_id, empty.history) == ("", [])


async def test_a_string_last_activity_reads_empty_and_keeps_the_document(store):
    docs(store)[SID] = {"history": [raw(TEXTS[0])], "owner_id": "u1", "created_at": 1.0, "last_activity": "yesterday"}
    assert (await store.load_session(SID)).history == []
    assert docs(store)[SID]["history"] == [raw(TEXTS[0])]


async def test_an_unreadable_document_reads_empty_without_raising(store):
    docs(store)[SID] = {"history": "broken", "owner_id": "u1", "created_at": 1.0, "last_activity": time.time()}
    assert (await store.load_session(SID)).history == []


async def test_a_backend_error_on_load_reads_empty_and_keeps_the_document(store, monkeypatch):
    await append_each(store, 1)

    async def down(*_a, **_k):
        raise ServiceUnavailable("503 UNAVAILABLE firestore down")

    monkeypatch.setattr(EvalDocRef, "get", down)
    assert (await store.load_session(SID)).history == []
    assert texts(store) == TEXTS[:1]


async def test_append_default_swallows_and_stores_nothing(store):
    store.db.raise_on_txn_read(SESS, SID, ServiceUnavailable("503 UNAVAILABLE firestore down"))
    assert await store.append_messages_batch(SID, [msg(TEXTS[0])], owner_id="u1") is None
    assert SID not in docs(store)


async def test_append_raise_on_error_reraises_and_stores_nothing(store):
    store.db.raise_on_txn_read(SESS, SID, ServiceUnavailable("503 UNAVAILABLE firestore down"))
    raised = None
    try:
        await store.append_messages_batch(SID, [msg(TEXTS[0])], owner_id="u1", raise_on_error=True)
    except ServiceUnavailable as exc:
        raised = exc
    assert raised is not None and SID not in docs(store)


async def test_save_session_swallows_a_backend_error(store, monkeypatch):
    async def down(*_a, **_k):
        raise ServiceUnavailable("503 UNAVAILABLE firestore down")

    monkeypatch.setattr(EvalDocRef, "set", down)
    assert await store.save_session(SID, SessionState(session_id=SID, history=[msg(TEXTS[0])])) is None
    assert SID not in docs(store)


async def test_a_failing_hand_off_leaves_the_append_stored(store, handoffs):
    async def broken(owner, session_id, messages):
        raise ServiceUnavailable("503 UNAVAILABLE firestore down")

    store.overflow_callback = broken
    await append_each(store, 5)
    await settle(store)
    assert texts(store) == TEXTS[2:5]


# --------------------------------------------------------------------------------------------------- latest session id

async def test_latest_session_id_is_the_most_recent_of_the_owner(store):
    docs(store)["u1:C1"] = {"history": [], "owner_id": "u1", "last_activity": 10.0}
    docs(store)["u1:C2"] = {"history": [], "owner_id": "u1", "last_activity": 30.0}
    docs(store)["u2:C3"] = {"history": [], "owner_id": "u2", "last_activity": 50.0}
    assert await store.get_latest_session_id("u1") == "u1:C2"
    assert await store.get_latest_session_id("u9") is None


async def test_latest_session_id_without_the_index_reads_none():
    store = make_store(db=EvalDb(indexes=[]))
    docs(store)["u1:C1"] = {"history": [], "owner_id": "u1", "last_activity": 10.0}
    assert await store.get_latest_session_id("u1") is None

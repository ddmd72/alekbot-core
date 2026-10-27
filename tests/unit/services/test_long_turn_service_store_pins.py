"""P11-G2-5 through `LongTurnService`: the real service over the real `FirestoreSessionStore` and
`FirestoreLongTurnRegistry` on the evaluating Firestore fake (H1).

Both session writes of a long turn (the 90 s mark pair and the late-answer pair) go through `retry_store_write`, which
retries only an exception, while the store swallows a transient error unless `raise_on_error=True` is passed; so the pair
is dropped and the retry never runs. The chat turn's own save is pinned in
`tests/unit/handlers/test_conversation_handler_store_pins.py`. The retry backoff is patched as a module attribute.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import ServiceUnavailable

import src.utils.retry as retry_module
from src.adapters.firestore_long_turn_registry import FirestoreLongTurnRegistry
from src.adapters.firestore_session_store import FirestoreSessionStore
from src.domain.llm import MessagePart
from src.services.long_turn_service import LongTurnService
from tests.support.firestore_eval import EvalDb
from tests.support.redelivery_world import require

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
SESS, TURNS = "eval_sessions", "eval_long_turns"
SID = "u1:C1"


@pytest.fixture
def world(monkeypatch):
    async def no_wait(delay):
        return None

    monkeypatch.setattr(retry_module, "asyncio", SimpleNamespace(sleep=no_wait))
    db = EvalDb(indexes=INDEXES_FILE, groups={SESS: "development_sessions"})
    store = FirestoreSessionStore(db, SESS, max_history_length=70, batch_size=50)
    service = LongTurnService(FirestoreLongTurnRegistry(db, TURNS), store)
    return SimpleNamespace(db=db, service=service)


def history(world):
    return [(m["role"], m["parts"][0]["text"]) for m in world.db.collection(SESS).docs[SID]["history"]]


async def save_late_answer(world):
    await world.service.save_late_answer(session_id=SID, owner_id="u1",
                                         note_text="[System: late answer to: When is the car service?]",
                                         history_text="On 2026-11-05.", full_text="The car service is on 2026-11-05.")


async def mark(world):
    await world.service.mark(turn_id="slack:Ev1", user_id="u1", session_id=SID, title="When is the car service?",
                             user_parts=[MessagePart(text="When is the car service?")], event_time=1000.0,
                             notice_text="Still working on it.", clock=world.service.new_clock(SID))


async def test_the_late_answer_pair_is_stored(world):
    await save_late_answer(world)
    assert history(world) == [("user", "[System: late answer to: When is the car service?]"),
                              ("model", "On 2026-11-05.")]


async def test_the_mark_pair_and_the_record_are_stored(world):
    await mark(world)
    assert history(world) == [("user", "When is the car service?"), ("model", "Still working on it.")]
    assert world.db.collection(TURNS).docs["slack:Ev1"]["status"] == "running"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "P11-G2-5: LongTurnService.save_late_answer retries only an exception, but the session store swallows a "
    "transient error by default, so the late-answer pair is dropped and the retry never runs"))
async def test_a_transient_error_in_the_late_answer_save_is_retried(world):
    """Flips with `raise_on_error=True` on the append in `LongTurnService.save_late_answer`."""
    world.db.raise_on_txn_read(SESS, SID, ServiceUnavailable("503 UNAVAILABLE transient"))
    await save_late_answer(world)
    require(world.db._raises.get((SESS, SID)) == [], "the transient error was raised once")
    assert SID in world.db.collection(SESS).docs


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "P11-G2-5: LongTurnService.mark retries only an exception, but the session store swallows a transient error "
    "by default, so the mark pair is dropped and the retry never runs"))
async def test_a_transient_error_in_the_mark_save_is_retried(world):
    """Flips with `raise_on_error=True` on the append in `LongTurnService.mark`."""
    world.db.raise_on_txn_read(SESS, SID, ServiceUnavailable("503 UNAVAILABLE transient"))
    await mark(world)
    require(world.db._raises.get((SESS, SID)) == [], "the transient error was raised once")
    require("slack:Ev1" in world.db.collection(TURNS).docs, "the turn record was written")
    assert SID in world.db.collection(SESS).docs

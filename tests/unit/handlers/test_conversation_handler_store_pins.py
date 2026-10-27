"""P11-G2-5 through the chat turn's history save: a real `ConversationHandler` over the real `FirestoreSessionStore` on
the evaluating Firestore fake (H1).

`_save_history_with_retry` wraps the append in `retry_store_write`, which retries only an exception; the store swallows
every error unless the caller passes `raise_on_error=True`, so a transient error drops the turn's pair from history and
the retry never runs. The same dead retry sits in `LongTurnService` (`tests/unit/services/test_long_turn_service_store_pins.py`).
The retry backoff is patched as a module attribute of `src.utils.retry`, never `asyncio.sleep` for the whole loop.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import ServiceUnavailable

import src.utils.retry as retry_module
from src.adapters.firestore_session_store import FirestoreSessionStore
from src.domain.llm import MessagePart
from src.handlers.conversation_handler import ConversationHandler
from tests.support.firestore_eval import EvalDb
from tests.support.redelivery_world import require

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
SESS = "eval_sessions"
SID = "u1:C1"


@pytest.fixture
def world(monkeypatch):
    async def no_wait(delay):
        return None

    monkeypatch.setattr(retry_module, "asyncio", SimpleNamespace(sleep=no_wait))
    db = EvalDb(indexes=INDEXES_FILE, groups={SESS: "development_sessions"})
    store = FirestoreSessionStore(db, SESS, max_history_length=70, batch_size=50)
    handler = ConversationHandler(coordinator=None, agent_factory=None, file_service=None)
    return SimpleNamespace(db=db, store=store, handler=handler)


async def save_turn(world):
    await world.handler._save_history_with_retry(
        session_store=world.store, session_id=SID, user_parts=[MessagePart(text="When is the car service?")],
        history_text="On 2026-11-05.", response_text="The car service is on 2026-11-05.", owner_id="u1")


async def test_the_turn_history_save_stores_the_pair(world):
    await save_turn(world)
    history = world.db.collection(SESS).docs[SID]["history"]
    assert [(m["role"], m["parts"][0]["text"]) for m in history] == [("user", "When is the car service?"),
                                                                      ("model", "On 2026-11-05.")]
    assert history[1]["parts"][0]["full_text"] == "The car service is on 2026-11-05."
    assert world.db.collection(SESS).docs[SID]["owner_id"] == "u1"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "P11-G2-5: the chat-turn history save retries only an exception, but the session store swallows a transient "
    "error by default, so the turn's pair is dropped and the retry never runs"))
async def test_a_transient_error_in_the_turn_history_save_is_retried(world):
    """Flips with `raise_on_error=True` on the append in `ConversationHandler._save_history_with_retry`."""
    world.db.raise_on_txn_read(SESS, SID, ServiceUnavailable("503 UNAVAILABLE transient"))
    await save_turn(world)
    require(world.db._raises.get((SESS, SID)) == [], "the transient error was raised once")
    assert SID in world.db.collection(SESS).docs

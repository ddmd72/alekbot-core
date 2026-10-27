"""P15 · J1: `$consolidate` against a message pair appended while it runs.

The command loads the session (conversation_handler.py:1575), trims the oldest batch in memory, acknowledges with the
`CONSOLIDATION_STARTED` UI string, enqueues the batch and saves the trimmed session whole (`save_session`, :1623). A pair
appended between the load and the save (a turn finishing in the same chat) is overwritten by that save.

The race is forced with an `asyncio.Event` gate in the response channel: it holds on the `CONSOLIDATION_STARTED` text
(after the load, before the save), the test appends the pair to completion, then releases, and asserts the gate was
reached. The session store is the real `FirestoreSessionStore` (`ReviewSessionStore`) on the H1 fake database; the queue,
the factory and the channel are fakes that keep what they were given.
"""
import asyncio
from typing import Any, List, Optional

import pytest

from src.domain.consolidation_run import ConsolidationRun
from src.domain.llm import Message, MessagePart
from src.domain.messaging import MessageContext
from src.domain.settings import ConsolidationSettings
from src.domain.ui_messages import UIMessage
from src.handlers.conversation_handler import ConversationHandler
from src.locales.uk import UI_STRINGS
from tests.support.fact_review_world import ReviewSessionStore
from tests.support.firestore_eval import EvalDb

USER = "u1"
SESSION = f"{USER}:C1"
STARTED = UI_STRINGS[UIMessage.CONSOLIDATION_STARTED.value]
GATE_TIMEOUT_S = 2.0
BATCH = 4


class _Queue:
    def __init__(self) -> None:
        self.batches: List[Any] = []

    async def enqueue_batch(self, batch) -> str:
        self.batches.append(batch)
        return f"b{len(self.batches)}"


class _UserRepo:
    async def get_user(self, user_id: str):
        return None  # the handler then uses its global consolidation settings


class _Factory:
    def __init__(self, store: ReviewSessionStore) -> None:
        self.store = store
        self.user_repo = _UserRepo()

    async def ensure_agents_for_user(self, user_id: str):
        return {}

    def get_session_store(self) -> ReviewSessionStore:
        return self.store


class _GatedChannel:
    """Keeps every text; holds on the `CONSOLIDATION_STARTED` acknowledgement while `gate_on_started`."""

    def __init__(self, gate_on_started: bool) -> None:
        self.gate_on_started = gate_on_started
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        self.texts: List[str] = []

    async def send_message(self, text: str, thread_id: Optional[str] = None) -> None:
        self.texts.append(text)
        if self.gate_on_started and text == STARTED:
            self.reached.set()
            await self.release.wait()

    async def send_status(self, status, thread_id: Optional[str] = None) -> None:
        return None


def _message(role: str, text: str) -> Message:
    return Message(role=role, parts=[MessagePart(text=text)])


async def _world():
    store = ReviewSessionStore(EvalDb(), "development_sessions")
    history = [_message("user" if i % 2 == 0 else "model", f"old message {i}") for i in range(6)]
    await store.append_messages_batch(SESSION, history, owner_id=USER, raise_on_error=True)
    queue = _Queue()

    async def consolidate_now(user_id: str) -> ConsolidationRun:
        return ConsolidationRun(processed=1)

    handler = ConversationHandler(
        coordinator=None, agent_factory=_Factory(store), file_service=None, consolidation_queue=queue,
        global_config=ConsolidationSettings(batch_size=BATCH), overflow_callback=consolidate_now,
    )
    context = MessageContext(text="$consolidate", session_id=SESSION, user_id=USER, account_id="a1",
                             metadata={"channel": "C1", "platform": "slack"})
    return store, queue, handler, context


PAIR = [_message("user", "remind me to read the Springfield news every morning"), _message("model", "Noted: Springfield news every morning.")]


async def _texts(store: ReviewSessionStore) -> List[str]:
    return [m.parts[0].text for m in await store.messages(SESSION)]


class TestConsolidateCommandInterleaving:

    async def test_control_a_pair_appended_before_the_command_survives_the_trim(self):
        store, queue, handler, context = await _world()
        await store.append_messages_batch(SESSION, PAIR, owner_id=USER, raise_on_error=True)
        channel = _GatedChannel(gate_on_started=False)

        await handler.handle_command("consolidate", context, channel)

        texts = await _texts(store)
        assert STARTED in channel.texts
        assert len(queue.batches) == 1 and len(queue.batches[0].messages) == BATCH
        assert texts[-2:] == [p.parts[0].text for p in PAIR]
        assert "old message 0" not in texts  # the oldest batch left the session

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
        "P15-S3: $consolidate saves the session it loaded before the run, overwriting a pair appended in "
        "between; owner decision"))
    async def test_a_pair_appended_while_the_command_runs_is_still_in_the_session(self):
        """Collections touched: `development_sessions` (the gate, not `yield_on_get`, orders the steps)."""
        store, queue, handler, context = await _world()
        channel = _GatedChannel(gate_on_started=True)

        command = asyncio.create_task(handler.handle_command("consolidate", context, channel))
        await asyncio.wait_for(channel.reached.wait(), GATE_TIMEOUT_S)
        await store.append_messages_batch(SESSION, PAIR, owner_id=USER, raise_on_error=True)
        channel.release.set()
        await command

        assert channel.reached.is_set()
        assert len(queue.batches) == 1 and len(queue.batches[0].messages) == BATCH
        texts = await _texts(store)
        assert "old message 0" not in texts  # the oldest batch left the session
        assert [p.parts[0].text for p in PAIR] == texts[-2:]

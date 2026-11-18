"""P11-G2-5 follow-up: the chat turn's history save now raises once its retries are spent (`raise_on_error=True`), and
`handle_message` keeps that failure away from the user: the answer is already delivered, so a lost history pair is
logged, never answered with the generic error reply.

The real `FirestoreSessionStore` on the evaluating fake, written through a companion binding (the path that writes the
session store); the transient error is queued for every attempt of `retry_store_write`.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from google.api_core.exceptions import ServiceUnavailable

import src.utils.retry as retry_module
from src.adapters.firestore_session_store import FirestoreSessionStore
from src.domain.agent import AgentResponse
from src.domain.channel_binding import ChannelBinding
from src.domain.companion_config import CompanionConfig
from src.domain.messaging import MessageContext, SmartResponse
from src.domain.ui_messages import StatusType
from src.handlers.conversation_handler import ConversationHandler
from tests.support.firestore_eval import EvalDb
from tests.support.redelivery_world import require

SESS = "eval_sessions"
WRITE_SID = "slack:C1"
ANSWER = "Bien! Sigamos."


def _handler(store: FirestoreSessionStore) -> ConversationHandler:
    agent_factory = MagicMock()
    agent_factory.ensure_agents_for_user = AsyncMock()
    agent_factory.get_session_store = MagicMock(return_value=store)
    coordinator = MagicMock()
    coordinator.handle_delegation = AsyncMock(return_value=AgentResponse.success(
        task_id="task-1", agent_id="tutor_agent_user-1", result=SmartResponse(text=ANSWER)))
    binding = MagicMock()
    binding.get = AsyncMock(return_value=ChannelBinding(
        channel_id="C1", agent_type="tutor", intent="tutor_chat", created_by="user-1",
        companion_config=CompanionConfig(window_threshold=50, batch_size=30)))
    return ConversationHandler(coordinator=coordinator, agent_factory=agent_factory, channel_binding_service=binding,
                               channel_history_source=MagicMock())


def _channel():
    ch = MagicMock()
    ch.platform = "slack"
    ch.send_status_with_phrase = AsyncMock(return_value=("msg-123", "thinking..."))
    ch.send_status = AsyncMock()
    ch.send_message = AsyncMock()
    ch.send_flat_response = AsyncMock()
    ch.send_chunked_message = AsyncMock()
    ch.update_status_with_phrase_and_dots = AsyncMock()
    ch.download_file = AsyncMock(return_value=None)
    ch.thread_id = None
    return ch


async def test_a_history_save_that_fails_every_retry_is_logged_not_answered_with_an_error(monkeypatch):
    async def no_wait(delay):
        return None

    monkeypatch.setattr(retry_module, "asyncio", SimpleNamespace(sleep=no_wait))
    db = EvalDb()
    store = FirestoreSessionStore(db, SESS, max_history_length=70, batch_size=50)
    for _ in range(3):
        db.raise_on_txn_read(SESS, WRITE_SID, ServiceUnavailable("503 UNAVAILABLE transient"))
    channel = _channel()
    context = MessageContext(text="Como se dice esto?", session_id="user-1:C1", user_id="user-1",
                             account_id="acc-1", metadata={"channel": "C1"})

    await _handler(store).handle_message(context, channel)

    require(db._raises.get((SESS, WRITE_SID)) == [], "every attempt of the history save failed")
    assert WRITE_SID not in db.collection(SESS).docs
    sent = [c.args[0] for c in channel.send_message.await_args_list]
    assert not any("Something went wrong" in text for text in sent)
    assert all(c.args[0] != StatusType.ERROR for c in channel.send_status.await_args_list)

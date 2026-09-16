"""
Unit test for the Phase G read-source switch: a companion binding
(ChannelBinding.companion_config set) makes ConversationHandler.handle_message
load history from SessionStore under mode.write_session_id, instead of the
platform API — mirroring the write side Phase F already wired.
"""
from unittest.mock import AsyncMock, MagicMock

from src.domain.agent import AgentResponse
from src.domain.channel_binding import ChannelBinding
from src.domain.companion_config import CompanionConfig
from src.domain.messaging import MessageContext, SmartResponse
from src.domain.llm import Message, MessagePart
from src.handlers.conversation_handler import ConversationHandler

_USER_ID = "user-1"
_ACCOUNT_ID = "acc-1"
_CHANNEL_ID = "C1"


def _make_handler(stored_history):
    session_store = MagicMock()
    session_store.append_messages_batch = AsyncMock()
    session_store.load_session = AsyncMock(
        return_value=MagicMock(history=stored_history)
    )

    agent_factory = MagicMock()
    agent_factory.ensure_agents_for_user = AsyncMock()
    agent_factory.get_session_store = MagicMock(return_value=session_store)

    coordinator = MagicMock()
    coordinator.handle_delegation = AsyncMock(
        return_value=AgentResponse.success(
            task_id="task-1", agent_id="tutor_agent_user-1",
            result=SmartResponse(text="Bien! Sigamos."),
        )
    )

    channel_binding = MagicMock()
    channel_binding.get = AsyncMock(
        return_value=ChannelBinding(
            channel_id=_CHANNEL_ID, agent_type="tutor", intent="tutor_chat",
            created_by=_USER_ID,
            companion_config=CompanionConfig(window_threshold=50, batch_size=30),
        )
    )

    channel_history = MagicMock()
    channel_history.fetch = AsyncMock(side_effect=AssertionError(
        "platform history must not be fetched for a companion (session_store) binding"
    ))

    handler = ConversationHandler(
        coordinator=coordinator, agent_factory=agent_factory, file_service=MagicMock(),
        channel_binding_service=channel_binding, channel_history_source=channel_history,
    )
    return handler, coordinator, session_store


def _make_context():
    return MessageContext(
        text="Como se dice esto?", session_id="user-1:C1", user_id=_USER_ID,
        account_id=_ACCOUNT_ID, metadata={"channel": _CHANNEL_ID},
    )


def _make_channel():
    ch = MagicMock()
    ch.platform = "slack"
    ch.send_status_with_phrase = AsyncMock(return_value=("msg-123", "thinking..."))
    ch.send_status = AsyncMock()
    ch.send_flat_response = AsyncMock()
    ch.send_chunked_message = AsyncMock()
    ch.update_status_with_phrase_and_dots = AsyncMock()
    ch.download_file = AsyncMock(return_value=None)
    ch.thread_id = None
    return ch


class TestCompanionReadsFromSessionStore:

    async def test_history_loaded_from_session_store_not_platform(self):
        stored = [Message(role="user", parts=[MessagePart(text="Hola profe")])]
        handler, coordinator, session_store = _make_handler(stored)

        await handler.handle_message(_make_context(), _make_channel())

        session_store.load_session.assert_awaited_once_with("slack:C1")
        coordinator.handle_delegation.assert_called_once()
        call_context = coordinator.handle_delegation.call_args.kwargs["context"]
        assert call_context["history"] == [m.model_dump() for m in stored]

    async def test_empty_session_produces_empty_history_no_crash(self):
        handler, coordinator, session_store = _make_handler([])
        await handler.handle_message(_make_context(), _make_channel())
        session_store.load_session.assert_awaited_once_with("slack:C1")
        call_context = coordinator.handle_delegation.call_args.kwargs["context"]
        assert "history" not in call_context or call_context["history"] == []

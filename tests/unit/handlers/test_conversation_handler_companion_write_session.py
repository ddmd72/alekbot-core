"""
Unit test for the Phase F write-path seam: ConversationHandler.handle_message's
"Save to History" call (conversation_handler.py:~884) passes
`session_id=mode.write_session_id or context.session_id`.

This is the one new seam Phase F's Task 1 introduced and it had no test
asserting the history save actually receives the companion session_id rather
than Alek's ambient context.session_id (Important #4, final whole-branch
review 2026-08-31). Deleting the `mode.write_session_id or` from that line
and reducing it back to `session_id=context.session_id` must fail this test.

Drives the full companion-bound flow through handle_message (not just
_resolve_session_mode in isolation) — a real ChannelBinding with
companion_config makes SessionMode.write_session_id a companion-shaped
"platform:channel_id" key, distinct from context.session_id.
"""
from unittest.mock import AsyncMock, MagicMock

from src.domain.agent import AgentResponse, AgentStatus
from src.domain.channel_binding import ChannelBinding
from src.domain.companion_config import CompanionConfig
from src.domain.messaging import MessageContext, SmartResponse
from src.handlers.conversation_handler import ConversationHandler

_USER_ID = "user-1"
_ACCOUNT_ID = "acc-1"
_CHANNEL_ID = "C1"
# Deliberately different from the companion write_session_id ("slack:C1") so
# the assertion actually distinguishes the two — this is Alek's own shape.
_AMBIENT_SESSION_ID = "user-1:C1"


def _make_handler():
    session_store = MagicMock()
    session_store.append_messages_batch = AsyncMock()

    agent_factory = MagicMock()
    agent_factory.ensure_agents_for_user = AsyncMock()
    agent_factory.get_session_store = MagicMock(return_value=session_store)

    coordinator = MagicMock()
    coordinator.handle_delegation = AsyncMock(
        return_value=AgentResponse.success(
            task_id="task-1",
            agent_id="tutor_agent_user-1",
            result=SmartResponse(text="Bien! Sigamos con el subjuntivo."),
        )
    )

    channel_binding = MagicMock()
    channel_binding.get = AsyncMock(
        return_value=ChannelBinding(
            channel_id=_CHANNEL_ID,
            agent_type="tutor",
            intent="tutor_chat",
            created_by=_USER_ID,
            companion_config=CompanionConfig(window_threshold=50, batch_size=30),
        )
    )

    handler = ConversationHandler(
        coordinator=coordinator,
        agent_factory=agent_factory,
        file_service=MagicMock(),
        channel_binding_service=channel_binding,
    )
    return handler, coordinator, session_store


def _make_context() -> MessageContext:
    return MessageContext(
        text="Como se dice 'I would go' en subjuntivo?",
        session_id=_AMBIENT_SESSION_ID,
        user_id=_USER_ID,
        account_id=_ACCOUNT_ID,
        metadata={"channel": _CHANNEL_ID},
    )


def _make_channel() -> MagicMock:
    ch = MagicMock()
    ch.platform = "slack"  # explicit string — MagicMock auto-attrs would poison getattr(..., "slack")
    ch.send_status_with_phrase = AsyncMock(return_value=("msg-123", "thinking..."))
    ch.send_status = AsyncMock()
    ch.send_flat_response = AsyncMock()
    ch.send_chunked_message = AsyncMock()
    ch.update_status_with_phrase_and_dots = AsyncMock()
    ch.download_file = AsyncMock(return_value=None)
    ch.thread_id = None
    return ch


class TestCompanionWriteSessionId:

    async def test_history_save_uses_companion_write_session_id_not_ambient(self):
        handler, coordinator, session_store = _make_handler()
        context = _make_context()
        channel = _make_channel()

        await handler.handle_message(context, channel)

        coordinator.handle_delegation.assert_called_once()
        session_store.append_messages_batch.assert_called_once()
        used_session_id = session_store.append_messages_batch.call_args[0][0]

        assert used_session_id == "slack:C1"
        assert used_session_id != _AMBIENT_SESSION_ID

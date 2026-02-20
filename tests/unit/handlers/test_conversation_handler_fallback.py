"""
Unit tests for ConversationHandler graceful degradation fallback.

Covers SmartAgent TIMEOUT/FAILED → QuickAgent direct fallback logic.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, call

from src.handlers.conversation_handler import ConversationHandler
from src.domain.messaging import MessageContext, SmartResponse
from src.domain.agent import AgentResponse, AgentStatus
from src.domain.ui_messages import StatusType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_agent_timeout() -> AgentResponse:
    return AgentResponse(
        task_id="task-1",
        agent_id="smart_response_agent_user-1",
        status=AgentStatus.TIMEOUT,
        result=None,
        confidence=0.0,
        error="Task execution timeout",
    )


def make_agent_failed() -> AgentResponse:
    return AgentResponse(
        task_id="task-1",
        agent_id="smart_response_agent_user-1",
        status=AgentStatus.FAILED,
        result=None,
        confidence=0.0,
        error="LLM provider error",
    )


def make_agent_success(text: str = "Bot reply") -> AgentResponse:
    return AgentResponse.success(
        task_id="task-1",
        agent_id="quick_response_agent_user-1",
        result=SmartResponse(text=text),
    )


def make_handler():
    """Build ConversationHandler with all required dependencies mocked."""
    session_store = MagicMock()
    session_store.append_messages_batch = AsyncMock()

    agent_factory = MagicMock()
    agent_factory.ensure_agents_for_user = AsyncMock()
    agent_factory.get_session_store = MagicMock(return_value=session_store)

    coordinator = MagicMock()
    coordinator.route_message = AsyncMock()

    handler = ConversationHandler(
        coordinator=coordinator,
        agent_factory=agent_factory,
        file_service=MagicMock(),
    )
    return handler, coordinator, session_store


def make_context() -> MessageContext:
    return MessageContext(
        text="This is a complex question",
        session_id="sess-test",
        user_id="user-test",
        account_id="acc-test",
    )


def make_channel() -> MagicMock:
    ch = MagicMock()
    ch.send_status_with_phrase = AsyncMock(return_value=("msg-123", "thinking..."))
    ch.send_status = AsyncMock()
    ch.send_message = AsyncMock()
    ch.send_chunked_message = AsyncMock()
    ch.update_message = AsyncMock()
    ch.update_status_with_phrase_and_dots = AsyncMock()
    ch.get_status_phrase = AsyncMock(return_value="processing...")
    ch.download_file = AsyncMock(return_value=None)
    ch.max_message_length = 4000
    ch.supports_message_editing = True
    return ch


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGracefulDegradationFallback:

    async def test_timeout_triggers_quickagent_fallback(self):
        """TIMEOUT from SmartAgent → QuickAgent called directly → user gets response."""
        handler, coordinator, session_store = make_handler()
        channel = make_channel()
        context = make_context()

        coordinator.route_message.side_effect = [
            make_agent_timeout(),
            make_agent_success("Вибач, трохи тормознув..."),
        ]

        await handler.handle_message(context, channel)

        assert coordinator.route_message.call_count == 2
        # Second call goes directly to QuickAgent, not Router
        second_msg = coordinator.route_message.call_args_list[1].args[0]
        assert second_msg.recipient == f"quick_response_agent_{context.user_id}"
        # System note injected in parts
        parts = second_msg.context["current_message_parts"]
        assert any("[System:" in (p.text or "") for p in parts)
        # User gets the text response, not an error
        channel.send_chunked_message.assert_called_once()
        channel.send_status.assert_not_called()

    async def test_failed_triggers_quickagent_fallback(self):
        """FAILED (not just TIMEOUT) also triggers fallback."""
        handler, coordinator, session_store = make_handler()
        channel = make_channel()
        context = make_context()

        coordinator.route_message.side_effect = [
            make_agent_failed(),
            make_agent_success("Ой, щось пішло не так..."),
        ]

        await handler.handle_message(context, channel)

        assert coordinator.route_message.call_count == 2
        second_msg = coordinator.route_message.call_args_list[1].args[0]
        assert second_msg.recipient == f"quick_response_agent_{context.user_id}"
        channel.send_chunked_message.assert_called_once()
        channel.send_status.assert_not_called()

    async def test_double_failure_shows_error_status_only_no_raw_text(self):
        """Both SmartAgent and QuickAgent fail → ERROR emoji only, no raw error text."""
        handler, coordinator, session_store = make_handler()
        channel = make_channel()
        context = make_context()

        coordinator.route_message.side_effect = [
            make_agent_timeout(),
            make_agent_timeout(),   # QuickAgent also times out
        ]

        await handler.handle_message(context, channel)

        assert coordinator.route_message.call_count == 2
        channel.send_status.assert_called_once_with(
            StatusType.ERROR, thread_id=context.thread_id
        )
        # Critical: no raw "Error: ..." text sent to user
        channel.send_message.assert_not_called()
        channel.send_chunked_message.assert_not_called()

    async def test_quickagent_exception_shows_error_status_only(self):
        """QuickAgent raises Exception → caught, ERROR status shown, no raw error text."""
        handler, coordinator, session_store = make_handler()
        channel = make_channel()
        context = make_context()

        coordinator.route_message.side_effect = [
            make_agent_timeout(),
            Exception("Connection refused"),   # QuickAgent explodes
        ]

        await handler.handle_message(context, channel)

        assert coordinator.route_message.call_count == 2
        channel.send_status.assert_called_with(StatusType.ERROR, thread_id=context.thread_id)
        channel.send_message.assert_not_called()

    async def test_no_fallback_on_success(self):
        """SUCCESS from SmartAgent → no fallback, route_message called exactly once."""
        handler, coordinator, session_store = make_handler()
        channel = make_channel()
        context = make_context()

        coordinator.route_message.return_value = make_agent_success("Great answer!")

        await handler.handle_message(context, channel)

        assert coordinator.route_message.call_count == 1
        channel.send_status.assert_not_called()
        channel.send_chunked_message.assert_called_once()

    async def test_system_note_does_not_mention_technical_details(self):
        """[System: ...] note instructs LLM not to expose error details."""
        handler, coordinator, session_store = make_handler()
        channel = make_channel()
        context = make_context()

        coordinator.route_message.side_effect = [
            make_agent_timeout(),
            make_agent_success("OK"),
        ]

        await handler.handle_message(context, channel)

        fallback_msg = coordinator.route_message.call_args_list[1].args[0]
        parts = fallback_msg.context["current_message_parts"]
        system_parts = [p.text for p in parts if p.text and "[System:" in p.text]
        assert system_parts, "Expected at least one [System: ...] note"
        note = system_parts[0]
        assert "Do NOT mention technical details" in note
        assert "apologize" in note

    async def test_fallback_preserves_original_message_parts(self):
        """User's original text is included in fallback parts alongside system note."""
        handler, coordinator, session_store = make_handler()
        channel = make_channel()
        context = make_context()

        coordinator.route_message.side_effect = [
            make_agent_timeout(),
            make_agent_success("OK"),
        ]

        await handler.handle_message(context, channel)

        fallback_msg = coordinator.route_message.call_args_list[1].args[0]
        parts = fallback_msg.context["current_message_parts"]
        user_parts = [p for p in parts if p.text and "[System:" not in p.text]
        assert user_parts, "Original user text should be in fallback parts"
        assert any(context.text in (p.text or "") for p in user_parts)

import pytest
from unittest.mock import AsyncMock, MagicMock
from src.handlers.conversation_handler import ConversationHandler
from src.domain.messaging import MessageContext

@pytest.mark.requirement("REQ-UI-07")
@pytest.mark.asyncio
async def test_explicit_command_protocol():
    """
    Test that explicit commands (starting with $) are handled correctly.
    Covers: REQ-UI-07 (Explicit Command Protocol)
    """
    # Setup
    mock_brain_service = AsyncMock()
    handler = ConversationHandler(
        coordinator=AsyncMock(),
        agent_factory=AsyncMock(),
        file_service=AsyncMock()
    )
    mock_response_channel = AsyncMock()
    
    context = MessageContext(
        text="$unknown_cmd",
        session_id="test-session",
        user_id="user-1",
        account_id="account-1",
        metadata={"event_type": "command"}
    )

    # Test unknown command to verify the protocol works
    await handler.handle_command("unknown_cmd", context, mock_response_channel)
    
    # Verify it sent an error message for unknown command
    mock_response_channel.send_message.assert_called_once()
    args, _ = mock_response_channel.send_message.call_args
    assert "Невідома команда" in args[0]
    assert "unknown_cmd" in args[0]

@pytest.mark.requirement("REQ-UI-07")
@pytest.mark.asyncio
async def test_consolidate_command_trigger():
    """
    Test that the consolidate command triggers the correct handler.
    Covers: REQ-UI-07 (Explicit Command Protocol)
    """
    # Setup
    mock_brain_service = AsyncMock()
    handler = ConversationHandler(
        coordinator=AsyncMock(),
        agent_factory=AsyncMock(),
        file_service=AsyncMock()
    )
    mock_response_channel = AsyncMock()
    
    context = MessageContext(
        text="$consolidate",
        session_id="test-session",
        user_id="user-1",
        account_id="account-1"
    )
    
    mock_overflow = AsyncMock()
    handler._overflow_callback = mock_overflow
    handler.consolidation_queue = AsyncMock()

    session_store_mock = AsyncMock()
    mock_session = MagicMock(messages=[1], user_id="user-1", session_id="test-session")
    session_store_mock.load_session.return_value = mock_session
    handler.agent_factory.get_session_store = MagicMock(return_value=session_store_mock)
    await handler.handle_command("consolidate", context, mock_response_channel)

    mock_overflow.assert_called_once()

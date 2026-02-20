import pytest
from unittest.mock import AsyncMock, MagicMock, call, patch
from src.handlers.conversation_handler import ConversationHandler
from src.domain.messaging import MessageContext
from src.domain.ui_messages import StatusType
from src.domain.agent import AgentResponse

@pytest.mark.requirement("REQ-UI-01")
@pytest.mark.requirement("REQ-UI-02")
@pytest.mark.asyncio
async def test_cognitive_transparency_and_streaming():
    """
    Test UI status updates (Transparency) and streaming (Performance).
    Covers: REQ-UI-01, REQ-UI-02
    """
    # Setup
    mock_brain_service = MagicMock()
    mock_brain_service.is_simple_request.return_value = True
    mock_brain_service.generate_quick_response = AsyncMock(return_value="Part 1 Part 2")
    mock_brain_service.append_user_message = AsyncMock()
    mock_brain_service.append_model_message = AsyncMock()

    mock_agent_factory = AsyncMock()
    mock_file_service = AsyncMock()
    
    handler = ConversationHandler(
        coordinator=AsyncMock(),
        agent_factory=mock_agent_factory,
        file_service=mock_file_service
    )
    mock_response_channel = AsyncMock()
    mock_response_channel.send_status_with_phrase.return_value = ("msg_id", "Thinking...")
    mock_response_channel.send_chunked_message = AsyncMock()
    
    context = MessageContext(
        text="Hello",
        session_id="test-session",
        user_id="user-1",
        account_id="account-1"
    )
    
    # Execute
    handler.coordinator.route_message.return_value = AgentResponse.success(
        task_id="t1",
        agent_id="quick_agent",
        result="Part 1 Part 2"
    )
    await handler.handle_message(context, mock_response_channel)
    
    # Verify REQ-UI-01: Status updates
    # Initial status
    mock_response_channel.send_status_with_phrase.assert_called_with(
        StatusType.THINKING, 
        thread_id=None
    )
    
    # Verify REQ-UI-02: Streaming updates
    # Streaming updates are handled via send_chunked_message in current implementation
    mock_response_channel.send_chunked_message.assert_called_once_with(
        "Part 1 Part 2",
        "msg_id",
        thread_id=None
    )

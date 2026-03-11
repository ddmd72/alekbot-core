import pytest
from unittest.mock import AsyncMock, MagicMock
from src.handlers.conversation_handler import ConversationHandler
from src.domain.messaging import MessageContext
from src.domain.agent import AgentResponse

@pytest.mark.requirement("REQ-UI-05")
@pytest.mark.asyncio
async def test_platform_agnostic_handling():
    """
    Test that ConversationHandler works with generic MessageContext, 
    proving independence from specific platforms (Slack/Telegram).
    Covers: REQ-UI-05 (Platform Fluidity)
    """
    mock_brain_service = MagicMock()
    mock_brain_service.is_simple_request.return_value = True
    mock_brain_service.generate_quick_response = AsyncMock(return_value="Response")
    mock_brain_service.append_user_message = AsyncMock()
    mock_brain_service.append_model_message = AsyncMock()
    
    handler = ConversationHandler(
        coordinator=AsyncMock(),
        agent_factory=AsyncMock(),
        file_service=AsyncMock()
    )
    mock_response_channel = AsyncMock()
    # Configure mock to return a tuple (message_id, phrase)
    mock_response_channel.send_status_with_phrase.return_value = ("msg_id", "Thinking...")
    mock_response_channel.send_chunked_message = AsyncMock()

    handler.coordinator.route_message.return_value = AgentResponse.success(
        task_id="t1",
        agent_id="quick_agent",
        result="OK"
    )
    
    # Create a generic context without any platform-specific metadata
    context = MessageContext(
        text="Generic message",
        session_id="generic-session",
        user_id="generic-user",
        account_id="account-1"
    )
    
    await handler.handle_message(context, mock_response_channel)
    
    # Verify processing succeeded
    handler.coordinator.route_message.assert_called_once()
    
    # Since the mock returns a simple string, we expect send_chunked_message to be called
    mock_response_channel.send_chunked_message.assert_called_once()

"""Unit test for ConversationHandler setting agent_context["interactive_delivery"].

Smart only offers draft_skill when this flag is true (Task 5). A normal (unbound)
conversational turn must carry it so the preview/save-command flow is reachable.
"""
from unittest.mock import AsyncMock, MagicMock, patch

from src.domain.agent import AgentResponse
from src.domain.messaging import MessageContext, SmartResponse
from src.domain.settings import ConsolidationSettings
from src.handlers.conversation_handler import ConversationHandler


def _handler(response):
    session_store = MagicMock()
    session_store.append_messages_batch = AsyncMock()
    session_store.load_session = AsyncMock(return_value=None)
    factory = MagicMock()
    factory.ensure_agents_for_user = AsyncMock()
    factory.get_session_store = MagicMock(return_value=session_store)
    factory.user_repo.get_user = AsyncMock(return_value=None)
    coordinator = MagicMock()
    coordinator.route_message = AsyncMock(return_value=response)
    handler = ConversationHandler(
        coordinator=coordinator, agent_factory=factory, file_service=MagicMock(),
        global_config=ConsolidationSettings(threshold=50, batch_size=30),
    )
    return handler, coordinator


def _channel():
    ch = MagicMock()
    ch.channel_id = "C-001"
    ch.platform = "slack"
    for name in ("send_status", "send_message", "send_chunked_message", "update_message",
                 "send_rich_content", "update_status_with_phrase_and_dots"):
        setattr(ch, name, AsyncMock())
    ch.send_status_with_phrase = AsyncMock(return_value=("m1", "thinking"))
    ch.get_status_phrase = AsyncMock(return_value="processing")
    ch.max_message_length = 4000
    ch.supports_message_editing = True
    return ch


def _ctx():
    return MessageContext(text="hello", session_id="s", user_id="u", account_id="a",
                          attachments=[], metadata={})


async def test_interactive_delivery_set_for_a_normal_message():
    response = AgentResponse.success(task_id="t", agent_id="smart", result=SmartResponse(text="Hi."))
    handler, coordinator = _handler(response)

    with patch.object(handler, "validate_model_output", side_effect=lambda t, u: t):
        await handler.handle_message(_ctx(), _channel())

    routed_message = coordinator.route_message.await_args.args[0]
    assert routed_message.context["interactive_delivery"] is True

"""A chat turn's id is fixed before Smart runs (DRIVE_FILE_NOTES_RFC §4.2, core contract §6): the platform's turn id
(`slack:<event_id>` / `telegram:<update_id>`) under TURN_ID_CONTEXT_KEY, else a fresh `chat:` id — never empty, and
never written back to `metadata.turn_id` (which turns the long-turn machinery on)."""
from unittest.mock import AsyncMock, MagicMock, patch

from src.domain.agent import AgentResponse
from src.domain.messaging import MessageContext, SmartResponse
from src.domain.settings import ConsolidationSettings
from src.domain.turn_identity import TURN_ID_CONTEXT_KEY
from src.handlers.conversation_handler import ConversationHandler


def _handler():
    response = AgentResponse.success(task_id="t", agent_id="smart", result=SmartResponse(text="Hi."))
    session_store = MagicMock()
    session_store.append_messages_batch = AsyncMock()
    session_store.load_session = AsyncMock(return_value=None)
    factory = MagicMock()
    factory.ensure_agents_for_user = AsyncMock()
    factory.get_session_store = MagicMock(return_value=session_store)
    factory.user_repo.get_user = AsyncMock(return_value=None)
    coordinator = MagicMock()
    coordinator.route_message = AsyncMock(return_value=response)
    handler = ConversationHandler(coordinator=coordinator, agent_factory=factory, file_service=MagicMock(),
                                  global_config=ConsolidationSettings(threshold=50, batch_size=30))
    return handler, coordinator


def _channel():
    ch = MagicMock()
    ch.channel_id = "C-001"
    ch.platform = "slack"
    for name in ("send_status", "send_message", "send_chunked_message", "update_message", "send_rich_content",
                 "update_status_with_phrase_and_dots"):
        setattr(ch, name, AsyncMock())
    ch.send_status_with_phrase = AsyncMock(return_value=("m1", "thinking"))
    ch.get_status_phrase = AsyncMock(return_value="processing")
    ch.max_message_length = 4000
    ch.supports_message_editing = True
    return ch


def _ctx(metadata):
    return MessageContext(text="hello", session_id="s", user_id="u", account_id="a", attachments=[],
                          metadata=metadata)


async def _routed_context(metadata):
    handler, coordinator = _handler()
    with patch.object(handler, "validate_model_output", side_effect=lambda t, u: t):
        await handler.handle_message(_ctx(metadata), _channel())
    return coordinator.route_message.await_args.args[0].context


async def test_the_platform_turn_id_is_the_turn_id():
    context = await _routed_context({"turn_id": "slack:Ev123"})
    assert context[TURN_ID_CONTEXT_KEY] == "slack:Ev123"


async def test_without_a_platform_turn_id_a_fresh_chat_id_is_fixed_and_metadata_is_untouched():
    metadata = {}
    context = await _routed_context(metadata)
    assert context[TURN_ID_CONTEXT_KEY].startswith("chat:") and len(context[TURN_ID_CONTEXT_KEY]) > len("chat:")
    assert "turn_id" not in metadata and "turn_id" not in context["metadata"]

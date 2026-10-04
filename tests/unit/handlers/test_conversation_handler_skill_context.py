"""Unit tests for ConversationHandler skill_context persistence.

Coverage targets:
  handle_message — skill_context from response.metadata → full_text (raw block) + summary (stub)
"""
from unittest.mock import AsyncMock, MagicMock, patch

from src.domain.agent import AgentResponse
from src.domain.messaging import MessageContext, SmartResponse
from src.domain.settings import ConsolidationSettings
from src.domain.skill import skill_stub
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
    return handler, session_store


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
    return MessageContext(text="status of IB123?", session_id="s", user_id="u", account_id="a",
                          attachments=[], metadata={})


async def test_skill_body_raw_in_full_text_and_stub_in_summary():
    response = AgentResponse.success(task_id="t", agent_id="smart", result=SmartResponse(text="Gate B12."))
    response.metadata["response_summary"] = "Gate given."
    response.metadata["skill_context"] = [{"name": "flight-status", "version": 2, "body": "1. Open *page*.\n2. Read `gate`."}]
    handler, store = _handler(response)

    with patch.object(handler, "validate_model_output", side_effect=lambda t, u: t):
        await handler.handle_message(_ctx(), _channel())

    model = next(m for m in store.append_messages_batch.call_args[0][1] if m.role == "model")
    part = model.parts[0]
    assert '[Skill "flight-status" v2]\n1. Open *page*.\n2. Read `gate`.' in part.full_text
    assert "skill_context" not in part.full_text           # not JSON-dumped by the generic loop
    assert part.text.endswith(skill_stub("flight-status"))  # summary carries the stub
    assert part.text.startswith("Gate given.")

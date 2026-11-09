"""Mutation survivors of conversation_handler (except-success): three best-effort handlers whose only observable effect is
the log line. Each test makes the guarded call raise and asserts the handled failure is logged, not silently dropped."""
from unittest.mock import AsyncMock, MagicMock, patch

from src.domain.messaging import MessageContext
from src.domain.settings import ConsolidationSettings
from src.handlers.conversation_handler import ConversationHandler

_LOGGER = "src.handlers.conversation_handler.logger"


def _handler(skill_service=None, coordinator=None) -> ConversationHandler:
    session_store = MagicMock()
    session_store.append_messages_batch = AsyncMock(side_effect=RuntimeError("store down"))
    session_store.load_session = AsyncMock(return_value=None)
    factory = MagicMock()
    factory.ensure_agents_for_user = AsyncMock()
    factory.get_session_store = MagicMock(return_value=session_store)
    factory.user_repo.get_user = AsyncMock(return_value=None)
    return ConversationHandler(coordinator=coordinator or MagicMock(), agent_factory=factory, file_service=MagicMock(),
                               global_config=ConsolidationSettings(threshold=50, batch_size=30),
                               skill_service=skill_service)


def _context(text: str = "hello") -> MessageContext:
    return MessageContext(text=text, session_id="s", user_id="user-test", account_id="acc-test",
                          attachments=[], metadata={})


def _channel() -> MagicMock:
    ch = MagicMock()
    ch.channel_id = "C-001"
    ch.platform = "slack"
    ch.language = "en"
    for name in ("send_status", "send_message", "send_chunked_message", "update_message", "send_rich_content",
                 "update_status_with_phrase_and_dots", "send_file"):
        setattr(ch, name, AsyncMock())
    ch.send_status_with_phrase = AsyncMock(return_value=("m1", "thinking"))
    ch.get_status_phrase = AsyncMock(return_value="processing")
    ch.max_message_length = 4000
    ch.supports_message_editing = True
    return ch


async def test_a_failed_error_reply_is_logged_as_a_warning():
    coordinator = MagicMock()
    coordinator.route_message = AsyncMock(side_effect=RuntimeError("agent down"))
    channel = _channel()
    channel.send_message = AsyncMock(side_effect=RuntimeError("slack down"))
    with patch(_LOGGER) as log:
        await _handler(coordinator=coordinator).handle_message(_context(), channel)
    assert any("Failed to send error response" in str(c.args[0]) for c in log.warning.call_args_list)


async def test_a_failed_late_answer_write_is_logged_with_the_session():
    long_turns = MagicMock()
    long_turns.save_late_answer = AsyncMock(side_effect=RuntimeError("store down"))
    with patch(_LOGGER) as log:
        await _handler()._close_long_turn_in_session(_context(), long_turns, None, "answer")
    assert any("closing the turn in session" in str(c.args[0]) and "s" in c.args[1:2]
               for c in log.error.call_args_list)


async def test_a_failed_skill_save_history_append_is_logged_after_the_confirmation():
    service = MagicMock()
    service.save_draft = AsyncMock(return_value=("alpha-skill", 3))
    channel = _channel()
    with patch(_LOGGER) as log:
        await _handler(skill_service=service)._handle_skill_save("7f3a", _context(), channel)
    assert channel.send_message.await_count == 1
    assert any("skill-save history pair" in str(c.args[0]) and "alpha-skill" in c.args
               for c in log.error.call_args_list)


async def test_origin_link_lookup_failure_is_logged_and_yields_none():
    channel = MagicMock()
    channel.message_link = AsyncMock(side_effect=RuntimeError("no link"))
    with patch(_LOGGER) as log:
        link = await ConversationHandler._origin_link(channel, "ts1")
    assert link is None
    assert any("origin link lookup failed" in str(c.args[0]) for c in log.warning.call_args_list)

"""
Unit tests for TelegramWebhookAdapter command dispatch ($command protocol).

Regression guard: "$consolidate" (and other $commands) from Telegram must be
routed to ConversationHandler.handle_command — NOT handle_message. Before the
fix, the Telegram adapter had no command branch (unlike the Slack adapters), so
"$consolidate" fell through to the agent stack as a normal message and the LLM
roleplayed a fake "consolidation complete" result without running anything.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.adapters.telegram.webhook_adapter import TelegramWebhookAdapter


def _make_adapter(conversation_handler, iam_service):
    """Build an adapter shell without touching Bot()/Blueprint setup."""
    adapter = object.__new__(TelegramWebhookAdapter)
    adapter.conversation_handler = conversation_handler
    adapter.iam_service = iam_service
    adapter.audio_service = None
    adapter._language_service = None
    adapter._localization = None
    adapter.bot = MagicMock()
    return adapter


def _make_message(text):
    message = MagicMock()
    message.from_user.id = 424242
    message.chat.id = 987654
    message.text = text
    message.caption = None
    message.is_topic_message = False
    message.message_thread_id = None
    message.photo = None
    message.document = None
    return message


def _authorized_decision():
    decision = MagicMock()
    decision.action = "allow"
    decision.user = MagicMock(user_id="user-1", account_id="account-1")
    return decision


@pytest.mark.asyncio
async def test_dollar_command_routes_to_handle_command():
    conversation_handler = AsyncMock()
    iam_service = AsyncMock()
    iam_service.authorize.return_value = _authorized_decision()
    adapter = _make_adapter(conversation_handler, iam_service)

    await adapter._process_message(_make_message("$consolidate"))

    conversation_handler.handle_command.assert_awaited_once()
    args, _ = conversation_handler.handle_command.call_args
    assert args[0] == "consolidate"
    conversation_handler.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_dollar_command_strips_prefix_and_lowercases():
    conversation_handler = AsyncMock()
    iam_service = AsyncMock()
    iam_service.authorize.return_value = _authorized_decision()
    adapter = _make_adapter(conversation_handler, iam_service)

    await adapter._process_message(_make_message("$Consolidate"))

    args, _ = conversation_handler.handle_command.call_args
    assert args[0] == "consolidate"


@pytest.mark.asyncio
async def test_command_context_carries_channel_for_binding():
    """$agent binding commands read context.metadata['channel'] — must be chat_id."""
    conversation_handler = AsyncMock()
    iam_service = AsyncMock()
    iam_service.authorize.return_value = _authorized_decision()
    adapter = _make_adapter(conversation_handler, iam_service)

    await adapter._process_message(_make_message("$agent off"))

    args, _ = conversation_handler.handle_command.call_args
    context = args[1]
    assert context.metadata["channel"] == "987654"
    assert context.metadata["platform"] == "telegram"
    assert context.session_id == "user-1:987654"


@pytest.mark.asyncio
async def test_normal_message_routes_to_handle_message():
    conversation_handler = AsyncMock()
    iam_service = AsyncMock()
    iam_service.authorize.return_value = _authorized_decision()
    adapter = _make_adapter(conversation_handler, iam_service)

    await adapter._process_message(_make_message("hello there"))

    conversation_handler.handle_message.assert_awaited_once()
    conversation_handler.handle_command.assert_not_called()

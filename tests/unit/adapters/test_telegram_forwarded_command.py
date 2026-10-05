"""
Unit tests for TelegramWebhookAdapter's command-dispatch guards added for Agent Skills:

1. Backtick strip — a copied `` `$skill save CODE` `` must still dispatch as a command
   (text pasted back verbatim from a `skill_preview` delivery, see
   src/domain/skill.py::SKILL_PREVIEW_DELIVERY), but backticks around only part of the
   text (e.g. a sentence quoting a command-looking token) must not.
2. Forwarded-message guard — a forwarded message is someone else's text, never the
   owner's command, even if it happens to start with "$".

Helpers are self-contained (no cross-test-file imports), mirroring the pattern used by
tests/unit/handlers/test_conversation_handler_skill_preview.py.
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


def _make_message(text, forward_origin=None):
    message = MagicMock()
    message.from_user.id = 424242
    message.chat.id = 987654
    message.text = text
    message.caption = None
    message.is_topic_message = False
    message.message_thread_id = None
    message.photo = None
    message.document = None
    message.voice = None
    message.audio = None
    message.forward_origin = forward_origin
    return message


def _authorized_decision():
    decision = MagicMock()
    decision.action = "allow"
    decision.user = MagicMock(user_id="user-1", account_id="account-1")
    return decision


# ---------------------------------------------------------------------------
# Backtick strip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_whole_text_wrapped_in_backticks_still_dispatches_as_command():
    conversation_handler = AsyncMock()
    iam_service = AsyncMock()
    iam_service.authorize.return_value = _authorized_decision()
    adapter = _make_adapter(conversation_handler, iam_service)

    await adapter._process_message(_make_message("`$skill save 7f3a`"))

    conversation_handler.handle_command.assert_awaited_once()
    args, _ = conversation_handler.handle_command.call_args
    assert args[0] == "skill save 7f3a"
    conversation_handler.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_backticks_around_a_substring_stay_a_normal_message():
    conversation_handler = AsyncMock()
    iam_service = AsyncMock()
    iam_service.authorize.return_value = _authorized_decision()
    adapter = _make_adapter(conversation_handler, iam_service)

    await adapter._process_message(_make_message("`$HOME` is wrong"))

    conversation_handler.handle_message.assert_awaited_once()
    conversation_handler.handle_command.assert_not_called()


@pytest.mark.asyncio
async def test_unclosed_backtick_stays_a_normal_message():
    """Only a leading backtick, no closing one — the whole text is NOT wrapped."""
    conversation_handler = AsyncMock()
    iam_service = AsyncMock()
    iam_service.authorize.return_value = _authorized_decision()
    adapter = _make_adapter(conversation_handler, iam_service)

    await adapter._process_message(_make_message("`$skill list"))

    conversation_handler.handle_message.assert_awaited_once()
    conversation_handler.handle_command.assert_not_called()


# ---------------------------------------------------------------------------
# Forwarded-message guard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_forwarded_dollar_command_is_not_dispatched_as_command():
    conversation_handler = AsyncMock()
    iam_service = AsyncMock()
    iam_service.authorize.return_value = _authorized_decision()
    adapter = _make_adapter(conversation_handler, iam_service)

    forwarded_marker = MagicMock()  # python-telegram-bot's MessageOrigin, any truthy value
    await adapter._process_message(_make_message("$skill save 7f3a", forward_origin=forwarded_marker))

    conversation_handler.handle_command.assert_not_called()
    conversation_handler.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_forwarded_dollar_command_still_dispatches():
    """Regression guard: the forward_origin check must not swallow ordinary commands."""
    conversation_handler = AsyncMock()
    iam_service = AsyncMock()
    iam_service.authorize.return_value = _authorized_decision()
    adapter = _make_adapter(conversation_handler, iam_service)

    await adapter._process_message(_make_message("$skill save 7f3a", forward_origin=None))

    conversation_handler.handle_command.assert_awaited_once()
    args, _ = conversation_handler.handle_command.call_args
    assert args[0] == "skill save 7f3a"
    conversation_handler.handle_message.assert_not_called()

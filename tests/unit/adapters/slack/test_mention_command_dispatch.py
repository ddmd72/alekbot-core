"""A `$` command inside a Slack @mention is dispatched as a command, never run as a chat turn (the message path does
the same in test_command_dispatch.py). An app_mention event carries no channel_type, so it is read as a channel."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.slack.http_adapter import HTTPModeAdapter
from src.domain.language import LanguageCode


def _make_adapter(conversation_handler, iam_service):
    adapter = object.__new__(HTTPModeAdapter)
    adapter.conversation_handler = conversation_handler
    adapter.iam_service = iam_service
    adapter.app = MagicMock()
    adapter.slack_bot_token = "xoxb-test"
    adapter._localization = None
    adapter._resolve_language = AsyncMock(return_value=LanguageCode.EN)
    return adapter


def _allowing_iam():
    iam = AsyncMock()
    decision = MagicMock()
    decision.action = "allow"
    decision.user = MagicMock(user_id="user-1", account_id="acct-1")
    iam.authorize.return_value = decision
    return iam


def _mention(text):
    return {"type": "app_mention", "text": text, "channel": "C0CHAN", "user": "U0SLACK", "ts": "1.1"}


async def test_a_dollar_command_in_a_mention_is_dispatched():
    handler = AsyncMock()
    adapter = _make_adapter(handler, _allowing_iam())

    await adapter._process_mention_event(_mention("<@U0BOT> $consolidate"), "ignored")

    handler.handle_message.assert_not_called()
    handler.handle_command.assert_awaited_once()
    command, ctx, _channel = handler.handle_command.call_args.args
    assert command == "consolidate"
    assert ctx.metadata["platform"] == "slack"
    assert ctx.metadata["channel"] == "C0CHAN"
    assert ctx.metadata["channel_type"] == "channel"
    assert ctx.thread_id == "1.1"


async def test_a_backticked_command_in_a_mention_is_dispatched():
    handler = AsyncMock()
    adapter = _make_adapter(handler, _allowing_iam())

    await adapter._process_mention_event(_mention("<@U0BOT> `$help`"), "ignored")

    handler.handle_message.assert_not_called()
    assert handler.handle_command.call_args.args[0] == "help"


async def test_a_mention_is_never_taken_for_a_dm():
    handler = AsyncMock()
    adapter = _make_adapter(handler, _allowing_iam())

    await adapter._process_mention_event(_mention("<@U0BOT> $geo"), "ignored")

    assert handler.handle_command.call_args.args[1].metadata["channel_type"] != "im"


async def test_a_plain_mention_still_runs_a_chat_turn():
    handler = AsyncMock()
    adapter = _make_adapter(handler, _allowing_iam())

    await adapter._process_mention_event(_mention("<@U0BOT> hello"), "ignored")

    handler.handle_message.assert_awaited_once()
    handler.handle_command.assert_not_called()


async def test_a_rejected_user_gets_no_command():
    handler = AsyncMock()
    iam = AsyncMock()
    decision = MagicMock()
    decision.action = "reject"
    decision.message = None
    iam.authorize.return_value = decision
    adapter = _make_adapter(handler, iam)

    await adapter._process_mention_event(_mention("<@U0BOT> $consolidate"), "ignored")

    handler.handle_message.assert_not_called()
    handler.handle_command.assert_not_called()

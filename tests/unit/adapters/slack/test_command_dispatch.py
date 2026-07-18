"""
Unit tests for Slack HTTP adapter $command dispatch metadata.

The consolidation report (and any command using UserNotificationService.notify)
must be able to route back to the originating channel. That requires the command
MessageContext.metadata to carry BOTH "platform" and "channel" — notify()'s
channel override only fires when both are present. This guards that contract.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.adapters.slack.http_adapter import HTTPModeAdapter
from src.domain.language import LanguageCode


def _make_adapter(conversation_handler, iam_service):
    adapter = object.__new__(HTTPModeAdapter)
    adapter.conversation_handler = conversation_handler
    adapter.iam_service = iam_service
    adapter.app = MagicMock()
    adapter.slack_bot_token = "xoxb-test"
    adapter._localization = None
    adapter._resolve_language = AsyncMock(return_value=(LanguageCode.EN, None, True))
    return adapter


def _authorized_decision():
    decision = MagicMock()
    decision.action = "allow"
    decision.user = MagicMock(user_id="user-1", account_id="account-1")
    return decision


@pytest.mark.asyncio
async def test_command_metadata_carries_platform_and_channel():
    conversation_handler = AsyncMock()
    iam_service = AsyncMock()
    iam_service.authorize.return_value = _authorized_decision()
    adapter = _make_adapter(conversation_handler, iam_service)

    event = {"text": "$consolidate", "channel": "C0ORIGIN", "user": "U0SLACK"}
    await adapter._process_message_event(event, session_id="ignored")

    conversation_handler.handle_command.assert_awaited_once()
    args, _ = conversation_handler.handle_command.call_args
    assert args[0] == "consolidate"
    context = args[1]
    assert context.metadata["platform"] == "slack"
    assert context.metadata["channel"] == "C0ORIGIN"
    conversation_handler.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_normal_message_not_treated_as_command():
    conversation_handler = AsyncMock()
    iam_service = AsyncMock()
    iam_service.authorize.return_value = _authorized_decision()
    adapter = _make_adapter(conversation_handler, iam_service)

    event = {"text": "hello there", "channel": "C0ORIGIN", "user": "U0SLACK"}
    await adapter._process_message_event(event, session_id="ignored")

    conversation_handler.handle_message.assert_awaited_once()
    conversation_handler.handle_command.assert_not_called()

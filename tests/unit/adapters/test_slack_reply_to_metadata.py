"""Slack HTTP adapter (Facts S4 · T5b): a message that replies inside a thread carries the thread's parent as
`reply_to_message_id`, so a reply to a posted question can be matched to it; a top-level message carries none."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.slack.http_adapter import HTTPModeAdapter
from src.domain.language import LanguageCode


def _adapter(conversation_handler):
    adapter = object.__new__(HTTPModeAdapter)
    adapter.conversation_handler = conversation_handler
    iam_service = AsyncMock()
    decision = MagicMock()
    decision.action = "allow"
    decision.user = MagicMock(user_id="user-1", account_id="account-1")
    iam_service.authorize.return_value = decision
    adapter.iam_service = iam_service
    adapter.app = MagicMock()
    adapter.slack_bot_token = "xoxb-test"
    adapter._localization = None
    adapter._resolve_language = AsyncMock(return_value=(LanguageCode.EN, None, True))
    return adapter


async def _message_metadata(event):
    handler = AsyncMock()
    await _adapter(handler)._process_message_event(event, session_id="ignored")
    handler.handle_message.assert_awaited_once()
    return handler.handle_message.call_args.args[0].metadata


async def _mention_metadata(event):
    handler = AsyncMock()
    await _adapter(handler)._process_mention_event(event, session_id="ignored")
    handler.handle_message.assert_awaited_once()
    return handler.handle_message.call_args.args[0].metadata


async def test_a_dm_thread_reply_carries_the_parent_thread_ts():
    event = {"text": "2", "channel": "D0OWNER", "user": "U0SLACK", "ts": "1760000200.000200",
             "thread_ts": "1760000100.000100"}
    assert (await _message_metadata(event))["reply_to_message_id"] == "1760000100.000100"


async def test_a_top_level_message_carries_no_reply_to():
    event = {"text": "hello", "channel": "D0OWNER", "user": "U0SLACK", "ts": "1760000200.000200"}
    assert "reply_to_message_id" not in await _message_metadata(event)


async def test_a_mention_inside_a_thread_carries_the_thread_ts_not_its_own_ts():
    event = {"text": "<@U0BOT> 2", "channel": "C0CHAN", "user": "U0SLACK", "ts": "1760000200.000200",
             "thread_ts": "1760000100.000100"}
    metadata = await _mention_metadata(event)
    assert metadata["reply_to_message_id"] == "1760000100.000100"
    assert metadata["origin_message_id"] == "1760000200.000200"


async def test_a_top_level_mention_carries_no_reply_to():
    event = {"text": "<@U0BOT> hi", "channel": "C0CHAN", "user": "U0SLACK", "ts": "1760000200.000200"}
    assert "reply_to_message_id" not in await _mention_metadata(event)


@pytest.mark.parametrize("event_name", ["message", "mention"])
async def test_a_thread_parent_is_not_a_reply_to_itself(event_name):
    event = {"text": "<@U0BOT> hi", "channel": "C0CHAN", "user": "U0SLACK", "ts": "1760000100.000100",
             "thread_ts": "1760000100.000100"}
    metadata = await (_message_metadata(event) if event_name == "message" else _mention_metadata(event))
    assert "reply_to_message_id" not in metadata

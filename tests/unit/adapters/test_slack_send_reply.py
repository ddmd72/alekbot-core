"""SlackResponseChannel.send_reply (Facts S4 · T8f): mocked at the SDK client boundary only."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.slack.response_channel import SlackResponseChannel


def _channel():
    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "1729760100.000200", "channel": "D1"})
    return SlackResponseChannel(client, "D1", "xoxb"), client


async def test_the_text_is_posted_in_the_thread_of_the_message_to_reply_to():
    ch, client = _channel()
    await ch.send_reply("Thank you, recorded.", "1729760000.000100")
    client.chat_postMessage.assert_awaited_once()
    kwargs = client.chat_postMessage.call_args.kwargs
    assert kwargs["channel"] == "D1" and kwargs["thread_ts"] == "1729760000.000100"
    assert "Thank you, recorded." in kwargs["text"]


async def test_a_slack_error_is_raised():
    ch, client = _channel()
    client.chat_postMessage.side_effect = RuntimeError("channel_not_found")
    with pytest.raises(RuntimeError, match="channel_not_found"):
        await ch.send_reply("hello", "1729760000.000100")

"""TelegramResponseChannel.send_reply (Facts S4 · T8f): mocked at the SDK `Bot` boundary only."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.telegram.response_channel import TelegramResponseChannel


async def test_the_text_goes_to_the_chat_as_a_reply_to_the_message_with_an_integer_id():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=321))
    sent = await TelegramResponseChannel(bot, 555).send_reply("Thank you, recorded.", "55")
    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == 555 and kwargs["text"] == "Thank you, recorded."
    assert kwargs["reply_to_message_id"] == 55 and isinstance(kwargs["reply_to_message_id"], int)
    assert sent == "321"


async def test_the_text_goes_out_literally_with_no_parse_mode():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    text = "Noted — *I'll* take it into account_ (`really`)."
    await TelegramResponseChannel(bot, 555).send_reply(text, "55")
    kwargs = bot.send_message.call_args.kwargs
    assert kwargs["text"] == text
    assert "parse_mode" in kwargs and kwargs["parse_mode"] is None  # absent would let a bot's Defaults apply


async def test_a_telegram_error_is_logged_and_raised(caplog):
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=RuntimeError("message to reply not found"))
    with pytest.raises(RuntimeError, match="message to reply not found"):
        await TelegramResponseChannel(bot, 555).send_reply("hello", "55")
    assert any("Failed to send reply to message 55" in r.getMessage() for r in caplog.records)

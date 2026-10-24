"""TelegramResponseChannel.send_message_ref (Facts S4 · T5b): mocked at the SDK `Bot` boundary only."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.telegram.response_channel import TelegramResponseChannel
from src.domain.messaging import MessageRef


async def test_the_ref_is_the_platform_the_chat_and_the_message_id_the_bot_returned():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=321))
    ref = await TelegramResponseChannel(bot, 555).send_message_ref("Is this still true?")
    assert ref == MessageRef(platform="telegram", channel_id="555", message_id="321")


async def test_the_text_goes_out_literally_with_no_parse_mode():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    text = "«Takes *vitamin_D* and `zinc`»  1 — yes"
    await TelegramResponseChannel(bot, 555).send_message_ref(text)
    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.call_args.kwargs
    assert kwargs["text"] == text
    assert kwargs["chat_id"] == 555
    assert kwargs.get("parse_mode") is None


async def test_a_telegram_error_is_raised():
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=RuntimeError("chat not found"))
    with pytest.raises(RuntimeError, match="chat not found"):
        await TelegramResponseChannel(bot, 555).send_message_ref("hello")

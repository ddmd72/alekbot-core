"""TelegramResponseChannel.send_literal (S4 review A4/R1): mocked at the SDK `Bot` boundary only."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.telegram.response_channel import TELEGRAM_CHUNK_SIZE, TelegramResponseChannel


def _bot(first_id: int = 10):
    bot = MagicMock()
    ids = iter(range(first_id, first_id + 100))
    bot.send_message = AsyncMock(side_effect=lambda **kw: MagicMock(message_id=next(ids)))
    return bot


async def test_the_text_is_sent_with_no_parse_mode_and_unchanged():
    bot = _bot()
    text = "Wipe <b>x</b> & *y* _z_ `w` [a](http://example.test) \\n"
    first = await TelegramResponseChannel(bot, 555).send_literal(text)
    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.call_args.kwargs
    assert kwargs["text"] == text and kwargs["chat_id"] == 555
    assert "parse_mode" in kwargs and kwargs["parse_mode"] is None  # absent would let a bot's Defaults apply
    assert "reply_to_message_id" not in kwargs
    assert first == "10"


async def test_a_reply_target_goes_to_the_first_message_only():
    bot = _bot()
    text = ("line one is here.\n\n" * 400)
    assert len(text) > TELEGRAM_CHUNK_SIZE
    await TelegramResponseChannel(bot, 555).send_literal(text, "55")
    calls = [c.kwargs for c in bot.send_message.call_args_list]
    assert len(calls) > 1
    assert calls[0]["reply_to_message_id"] == 55 and isinstance(calls[0]["reply_to_message_id"], int)
    assert all("reply_to_message_id" not in c for c in calls[1:])
    assert all(c["parse_mode"] is None and len(c["text"]) <= TELEGRAM_CHUNK_SIZE for c in calls)


async def test_a_telegram_error_is_logged_and_raised(caplog):
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=RuntimeError("chat not found"))
    with pytest.raises(RuntimeError, match="chat not found"):
        await TelegramResponseChannel(bot, 555).send_literal("hello")
    assert any("Failed to send literal message" in r.getMessage() for r in caplog.records)


async def test_empty_text_is_refused():
    bot = _bot()
    with pytest.raises(ValueError):
        await TelegramResponseChannel(bot, 555).send_literal("")
    bot.send_message.assert_not_awaited()

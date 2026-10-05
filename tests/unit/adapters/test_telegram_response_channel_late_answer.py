from unittest.mock import AsyncMock, MagicMock

from src.adapters.telegram.response_channel import TelegramResponseChannel


async def test_late_answer_replies_to_the_origin_message():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=77))
    ch = TelegramResponseChannel(bot, 42)
    ch.send_chunked_message = AsyncMock()
    await ch.send_late_answer("the answer", "[late answer]", "15")
    assert bot.send_message.call_args.kwargs["reply_to_message_id"] == 15
    text, msg_id = ch.send_chunked_message.call_args.args[:2]
    assert text.startswith("[late answer]") and "the answer" in text and msg_id == "77"


async def test_message_link_is_none_and_hook_is_noop():
    ch = TelegramResponseChannel(MagicMock(), 42)
    assert await ch.message_link("15") is None
    await ch.on_long_turn()


async def test_late_answer_accepts_a_link_kwarg():
    # Final review M5: the protocol carries `link`; Telegram replies instead of linking.
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=77))
    ch = TelegramResponseChannel(bot, 42)
    ch.send_chunked_message = AsyncMock()
    await ch.send_late_answer("the answer", "[late answer]", "15", link=None)
    assert bot.send_message.call_args.kwargs["reply_to_message_id"] == 15

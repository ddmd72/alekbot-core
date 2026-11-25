"""Telegram: an answer with more than two distinct links is sent and edited with previews disabled (wire level)."""
from unittest.mock import AsyncMock, MagicMock

from telegram import LinkPreviewOptions

from src.adapters.telegram.response_channel import TelegramResponseChannel

L1, L2, L3 = "https://example.com/1", "https://example.org/2", "https://example.net/3"


def _channel():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=7))
    bot.edit_message_text = AsyncMock()
    return TelegramResponseChannel(bot, 42), bot


def _long(first_paragraph: str) -> str:
    return first_paragraph + " " + "a" * 1500 + "\n\n" + "b" * 1500 + "\n\n" + "c" * 1500


def _off(kwargs) -> bool:
    opts = kwargs.get("link_preview_options")
    return isinstance(opts, LinkPreviewOptions) and opts.is_disabled is True


async def test_three_links_chunked_edit_and_sends_all_disabled():
    ch, bot = _channel()
    await ch.send_chunked_message(_long(f"{L1} {L2} {L3}"), "5")
    sends = [c.kwargs for c in bot.send_message.call_args_list]
    edits = [c.kwargs for c in bot.edit_message_text.call_args_list]
    assert len(edits) == 1 and len(sends) >= 1
    assert _off(edits[0]) and all(_off(k) for k in sends)


async def test_two_links_chunked_no_preview_kwargs():
    ch, bot = _channel()
    await ch.send_chunked_message(_long(f"{L1} {L2}"), "5")
    kws = [c.kwargs for c in bot.send_message.call_args_list + bot.edit_message_text.call_args_list]
    assert kws and all("link_preview_options" not in k for k in kws)


async def test_single_message_three_links_disabled_two_links_default_shape():
    ch, bot = _channel()
    await ch.send_message(f"{L1} {L2} {L3}")
    assert _off(bot.send_message.call_args.kwargs)
    bot.send_message.reset_mock()
    await ch.send_message(f"{L1} {L2}")
    assert "link_preview_options" not in bot.send_message.call_args.kwargs


async def test_anchors_via_link_list_count():
    ch, bot = _channel()
    links = [{"anchor": i, "title": f"t{i}", "url": u} for i, u in enumerate([L1, L2, L3], start=1)]
    await ch.send_message("see [1] [2] [3]", link_list=links)
    assert _off(bot.send_message.call_args.kwargs)


async def test_markdown_failure_plain_text_fallback_send_carries_flag():
    ch, bot = _channel()
    bot.send_message.side_effect = [Exception("can't parse entities"), MagicMock(message_id=8)]
    await ch.send_message(f"{L1} {L2} {L3}")
    first, fallback = (c.kwargs for c in bot.send_message.call_args_list)
    assert first["parse_mode"] == "MarkdownV2" and _off(first)
    assert fallback["parse_mode"] is None and _off(fallback)


async def test_markdown_failure_plain_text_fallback_edit_carries_flag():
    ch, bot = _channel()
    bot.edit_message_text.side_effect = [Exception("can't parse entities"), None]
    await ch.update_message("5", f"{L1} {L2} {L3}")
    first, fallback = (c.kwargs for c in bot.edit_message_text.call_args_list)
    assert _off(first) and fallback["parse_mode"] is None and _off(fallback)


async def test_edit_too_old_falls_back_to_new_message_with_flag():
    ch, bot = _channel()
    bot.edit_message_text.side_effect = Exception("message can't be edited")
    await ch.update_message("5", f"{L1} {L2} {L3}")
    assert _off(bot.send_message.call_args.kwargs)


async def test_long_text_overflow_all_posts_disabled():
    ch, bot = _channel()
    await ch.send_long_text(_long(f"{L1} {L2} {L3}"))
    sends = [c.kwargs for c in bot.send_message.call_args_list]
    assert len(sends) >= 2 and all(_off(k) for k in sends)


async def test_flat_response_edit_and_sends_disabled():
    ch, bot = _channel()
    await ch.send_flat_response(_long(f"{L1} {L2} {L3}"), "5")
    assert _off(bot.edit_message_text.call_args.kwargs)
    assert all(_off(c.kwargs) for c in bot.send_message.call_args_list)


async def test_literal_three_links_disabled_two_default():
    ch, bot = _channel()
    await ch.send_literal(f"{L1} {L2} {L3}", "5")
    kw = bot.send_message.call_args.kwargs
    assert _off(kw) and kw["reply_to_message_id"] == 5
    bot.send_message.reset_mock()
    await ch.send_literal(f"{L1} {L2}")
    assert "link_preview_options" not in bot.send_message.call_args.kwargs


async def test_late_answer_three_links_carries_decision_to_every_part():
    ch, bot = _channel()
    await ch.send_late_answer(_long(f"{L1} {L2} {L3}"), "[late]", "15")
    header = bot.send_message.call_args_list[0].kwargs
    assert "link_preview_options" not in header
    assert _off(bot.edit_message_text.call_args.kwargs)
    assert all(_off(c.kwargs) for c in bot.send_message.call_args_list[1:])


async def test_raw_escaped_urls_are_counted_after_unescaping():
    ch, _ = _channel()
    assert ch._previews_allowed(f"{L1} {L1} {L2}", None) is True
    assert ch._previews_allowed(f"{L1} {L2} {L3}", None) is False

from unittest.mock import AsyncMock, MagicMock

from src.adapters.slack.response_channel import SlackResponseChannel


def _channel(**kw):
    client = MagicMock()
    client.chat_getPermalink = AsyncMock(return_value={"ok": True, "permalink": "https://s/p1"})
    client.chat_postMessage = AsyncMock(return_value={"ts": "111.2"})
    client.chat_update = AsyncMock(return_value={"ok": True})
    return SlackResponseChannel(client, "D1", "xoxb", **kw), client


async def test_message_link_uses_permalink():
    ch, client = _channel()
    assert await ch.message_link("100.1") == "https://s/p1"
    client.chat_getPermalink.assert_awaited_with(channel="D1", message_ts="100.1")


async def test_message_link_survives_api_error():
    ch, client = _channel()
    client.chat_getPermalink.side_effect = RuntimeError("slack down")
    assert await ch.message_link("100.1") is None


async def test_late_answer_goes_to_main_feed_with_prefix_and_link():
    ch, client = _channel()
    ch.send_chunked_message = AsyncMock()
    await ch.send_late_answer("the answer", "[late answer]", "100.1")
    first = client.chat_postMessage.call_args.kwargs
    assert first.get("thread_ts") is None
    text = ch.send_chunked_message.call_args.args[0]
    assert text.startswith("[late answer]") and "https://s/p1" in text and "the answer" in text


async def test_late_answer_without_link_still_delivered():
    ch, client = _channel()
    client.chat_getPermalink.side_effect = RuntimeError("x")
    ch.send_chunked_message = AsyncMock()
    await ch.send_late_answer("the answer", "[late answer]", "100.1")
    assert "the answer" in ch.send_chunked_message.call_args.args[0]


async def test_on_long_turn_calls_the_hook_once():
    hook = MagicMock()
    ch, _ = _channel(on_long_turn=hook)
    await ch.on_long_turn()
    await ch.on_long_turn()
    hook.assert_called_once()

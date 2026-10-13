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
    await ch.send_late_answer("the answer", "[late answer]", "100.1")
    # Short answer: two top-level posts — the header with the link first, then the body —
    # never edited afterwards.
    assert client.chat_postMessage.await_count == 2
    header, body = (c.kwargs for c in client.chat_postMessage.call_args_list)
    assert header.get("thread_ts") is None and body.get("thread_ts") is None
    client.chat_update.assert_not_awaited()
    assert header["text"].startswith("[late answer]") and "https://s/p1" in header["text"]
    assert "the answer" in body["text"]


async def test_late_answer_without_link_still_delivered():
    ch, client = _channel()
    client.chat_getPermalink.side_effect = RuntimeError("x")
    await ch.send_late_answer("the answer", "[late answer]", "100.1")
    client.chat_update.assert_not_awaited()
    posted_text = client.chat_postMessage.call_args.kwargs["text"]
    assert "the answer" in posted_text


async def test_late_answer_overflow_stays_in_main_feed_not_threaded():
    """Owner decision: the late answer never moves to a thread, even on overflow.

    Exercises the REAL chunking path (_resolve_and_split / send_message are NOT
    mocked) — regression guard for the bug where overflow fell back to
    send_chunked_message's thread_ts=message_id threading.
    """
    ch, client = _channel()
    long_answer = "A" * 2500  # exceeds SLACK_CHUNK_SIZE (2000) — forces multi-chunk delivery
    await ch.send_late_answer(long_answer, "[late answer]", "100.1")

    # Every chat_postMessage call (header chunk + overflow chunks) must carry no thread_ts.
    assert client.chat_postMessage.await_count >= 3  # the header, then a body that really overflowed
    for call in client.chat_postMessage.call_args_list:
        assert call.kwargs.get("thread_ts") is None

    # Nothing is posted-then-edited.
    client.chat_update.assert_not_awaited()

    # The first post starts with the prefix.
    first_post_text = client.chat_postMessage.call_args_list[0].kwargs["text"]
    assert first_post_text.startswith("[late answer]")

    # All of the answer's content is delivered across the posted chunks
    # (order-independent: just verify nothing was dropped).
    delivered = "".join(call.kwargs["text"] for call in client.chat_postMessage.call_args_list)
    assert delivered.count("A") == 2500


async def test_late_answer_resolves_link_list_anchors():
    """link_list anchors are resolved in the late answer body, same as a normal answer."""
    ch, client = _channel()
    link_list = [{"anchor": "1", "title": "Example", "url": "https://ex.com"}]
    await ch.send_late_answer("see [1] for details", "[late answer]", "100.1", link_list=link_list)
    client.chat_update.assert_not_awaited()
    posted_text = client.chat_postMessage.call_args.kwargs["text"]
    assert "<https://ex.com|Example>" in posted_text


async def test_on_long_turn_calls_the_hook_once():
    hook = MagicMock()
    ch, _ = _channel(on_long_turn=hook)
    await ch.on_long_turn()
    await ch.on_long_turn()
    hook.assert_called_once()


# --- Final review M5: a link fetched by the caller is not fetched again -------------


async def test_late_answer_with_a_given_link_does_not_refetch_the_permalink():
    ch, client = _channel()
    await ch.send_late_answer("the answer", "[late answer]", "100.1", link="https://s/given")
    client.chat_getPermalink.assert_not_awaited()
    client.chat_update.assert_not_awaited()
    header, body = (c.kwargs["text"] for c in client.chat_postMessage.call_args_list)
    assert "https://s/given" in header and "the answer" in body


async def test_late_answer_without_a_given_link_still_fetches_it():
    ch, client = _channel()
    await ch.send_late_answer("the answer", "[late answer]", "100.1", link=None)
    client.chat_getPermalink.assert_awaited_once()

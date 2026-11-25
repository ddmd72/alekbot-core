"""Slack: an answer with more than two distinct links is posted without unfurls (wire level: chat_postMessage kwargs)."""
from unittest.mock import AsyncMock, MagicMock

from src.adapters.slack.response_channel import SlackResponseChannel

NO_UNFURL = {"unfurl_links": False, "unfurl_media": False}
L1, L2, L3 = "https://example.com/1", "https://example.org/2", "https://example.net/3"


def _channel():
    client = MagicMock()
    client.chat_getPermalink = AsyncMock(return_value={"ok": True, "permalink": "https://origin.test/p1"})
    client.chat_postMessage = AsyncMock(return_value={"ts": "111.2"})
    client.chat_update = AsyncMock(return_value={"ok": True})
    return SlackResponseChannel(client, "D1", "xoxb"), client


def _long(first_paragraph: str) -> str:
    """Two paragraphs that cannot share one 2000-char chunk; links only in the first."""
    return first_paragraph + " " + "a" * 1500 + "\n\n" + "b" * 1500


def _posts(client):
    return [c.kwargs for c in client.chat_postMessage.call_args_list]


async def test_three_links_chunked_every_chunk_unfurl_off_even_without_links():
    ch, client = _channel()
    await ch.send_chunked_message(_long(f"{L1} {L2} {L3}"), "100.1")
    posts = _posts(client)
    assert len(posts) == 2
    assert all(p["unfurl_links"] is False and p["unfurl_media"] is False for p in posts)
    assert "https://" not in posts[1]["text"]  # the second chunk holds no link of its own


async def test_two_links_chunked_no_unfurl_kwargs():
    ch, client = _channel()
    await ch.send_chunked_message(_long(f"{L1} {L2}"), "100.1")
    posts = _posts(client)
    assert len(posts) == 2
    assert all("unfurl_links" not in p and "unfurl_media" not in p for p in posts)


async def test_send_message_single_three_links_unfurl_off():
    ch, client = _channel()
    await ch.send_message(f"{L1} {L2} {L3}")
    assert {k: v for k, v in _posts(client)[0].items() if k.startswith("unfurl")} == NO_UNFURL


async def test_send_message_two_links_keeps_default_call_shape():
    ch, client = _channel()
    await ch.send_message(f"{L1} {L2}", thread_id="9.9")
    assert _posts(client) == [{"channel": "D1", "text": f"{L1} {L2}", "thread_ts": "9.9", "mrkdwn": True}]


async def test_anchors_resolved_via_link_list_count_toward_the_total():
    ch, client = _channel()
    links = [{"anchor": i, "title": f"t{i}", "url": u} for i, u in enumerate([L1, L2, L3], start=1)]
    await ch.send_message("see [1] [2] [3]", link_list=links)
    assert _posts(client)[0]["unfurl_links"] is False


async def test_late_answer_header_keeps_previews_chunks_share_the_answer_decision():
    ch, client = _channel()
    await ch.send_late_answer(_long(f"{L1} {L2} {L3}"), "[late]", "100.1")
    posts = _posts(client)
    assert len(posts) == 3
    assert "origin.test" in posts[0]["text"] and "unfurl_links" not in posts[0]
    assert all(p["unfurl_links"] is False and p["unfurl_media"] is False for p in posts[1:])


async def test_late_answer_with_two_links_has_no_flags():
    ch, client = _channel()
    await ch.send_late_answer(_long(f"{L1} {L2}"), "[late]", "100.1")
    assert all("unfurl_links" not in p for p in _posts(client))


async def test_flat_response_three_links_overflow_posts_unfurl_off():
    ch, client = _channel()
    await ch.send_flat_response(_long(f"{L1} {L2} {L3}"), "100.1")
    posts = _posts(client)
    assert len(posts) == 1 and posts[0]["unfurl_links"] is False
    client.chat_update.assert_awaited_once()
    assert "unfurl_links" not in client.chat_update.call_args.kwargs


async def test_long_text_overflow_three_links_chunks_unfurl_off():
    ch, client = _channel()
    await ch.send_long_text(_long(f"{L1} {L2} {L3}"))
    posts = _posts(client)
    assert len(posts) == 3  # placeholder + 2 chunks
    assert all(p["unfurl_links"] is False for p in posts[1:])


async def test_literal_chunks_share_one_decision():
    ch, client = _channel()
    await ch.send_literal(_long(f"{L1} {L2} {L3}"), "100.1")
    posts = _posts(client)
    assert len(posts) == 2 and all(p["unfurl_links"] is False and p["mrkdwn"] is False for p in posts)


async def test_literal_two_links_no_flags():
    ch, client = _channel()
    await ch.send_literal(f"{L1} {L2}")
    assert "unfurl_links" not in _posts(client)[0]


async def test_update_message_never_gets_unfurl_kwargs():
    ch, client = _channel()
    await ch.update_message("100.1", f"{L1} {L2} {L3}")
    assert "unfurl_links" not in client.chat_update.call_args.kwargs

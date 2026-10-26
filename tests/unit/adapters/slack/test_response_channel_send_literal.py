"""SlackResponseChannel.send_literal and send_reply (S4 review A4/R1): text of unknown origin is SHOWN, never obeyed.

Two layers. The real `slack_sdk` `AsyncWebClient` posts to a local HTTP server, so the assertion is on the bytes the Slack
Web API would receive; and the SDK client method is mocked for the cases that need no wire (chunking, errors)."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from slack_sdk.web.async_client import AsyncWebClient

from src.adapters.slack.mrkdwn_text import escape_literal
from src.adapters.slack.response_channel import SLACK_CHUNK_SIZE, SlackResponseChannel

HOSTILE = "Wipe <!channel> and <@U1ABC>, see <https://example.test|click> & R&D; a_b_c *bold* `code`\\n"


class FakeSlackApi:
    """A local stand-in for slack.com: records every chat.postMessage body as the SDK sent it."""

    def __init__(self) -> None:
        self.posts: list = []

    async def post_message(self, request: web.Request) -> web.Response:
        if request.content_type == "application/json":
            body = await request.json()
        else:
            body = dict(await request.post())
        self.posts.append(body)
        return web.json_response({"ok": True, "channel": "D1", "ts": f"1729760100.{len(self.posts):06d}"})


async def _wire_channel():
    api = FakeSlackApi()
    app = web.Application()
    app.router.add_post("/chat.postMessage", api.post_message)
    server = TestServer(app)
    await server.start_server()
    client = AsyncWebClient(token="xoxb-test", base_url=str(server.make_url("/")))
    return SlackResponseChannel(client, "D1", "xoxb-test"), api, server


async def test_on_the_wire_nothing_in_the_text_is_left_for_slack_to_obey():
    channel, api, server = await _wire_channel()
    try:
        await channel.send_literal(HOSTILE)
    finally:
        await server.close()
    assert len(api.posts) == 1
    text = api.posts[0]["text"]
    for control in ("<!channel>", "<@U1ABC>", "<https://example.test|click>"):
        assert control not in text
    assert "&lt;!channel&gt;" in text and "&lt;@U1ABC&gt;" in text and "R&amp;D;" in text
    assert text == escape_literal(HOSTILE)  # and nothing else was rewritten, the literal \n included
    assert str(api.posts[0]["mrkdwn"]).lower() == "false"


async def test_on_the_wire_a_reply_goes_to_the_thread_and_is_literal():
    channel, api, server = await _wire_channel()
    try:
        await channel.send_reply("Done <!here>", "1729760000.000100")
    finally:
        await server.close()
    post = api.posts[0]
    assert post["thread_ts"] == "1729760000.000100" and post["text"] == "Done &lt;!here&gt;"


async def test_the_markdown_path_still_obeys_so_the_literal_path_is_the_fix():
    """Pins why send_message is not used for fact text: it leaves `<!channel>` as written."""
    channel, api, server = await _wire_channel()
    try:
        await channel.send_message("Wipe <!channel>")
    finally:
        await server.close()
    assert "<!channel>" in api.posts[0]["text"]


def _mocked_channel():
    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True, "channel": "D1", "ts": "1.1"})
    return SlackResponseChannel(client, "U1", "xoxb"), client


async def test_a_long_text_is_split_before_escaping_and_every_chunk_is_whole():
    ch, client = _mocked_channel()
    paragraph = "a & b < c > d. " * 60 + "\n\n"
    text = paragraph * 6
    await ch.send_literal(text, "9.9")
    posts = [c.kwargs for c in client.chat_postMessage.call_args_list]
    assert len(posts) > 1
    assert all(p["thread_ts"] == "9.9" and p["mrkdwn"] is False for p in posts)
    assert all("&amp;amp;" not in p["text"] and "<" not in p["text"] and ">" not in p["text"] for p in posts)
    assert all(len(p["text"]) <= SLACK_CHUNK_SIZE * 5 for p in posts)
    rebuilt = "".join(p["text"] for p in posts).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    assert rebuilt.replace("\n", "").replace(" ", "") == text.replace("\n", "").replace(" ", "")


async def test_the_dm_channel_id_a_user_id_opened_is_kept_for_later_calls():
    ch, _ = _mocked_channel()
    await ch.send_literal("hello")
    assert ch.channel_id == "D1"


async def test_the_first_response_is_returned_and_a_slack_error_is_raised():
    ch, client = _mocked_channel()
    assert (await ch.send_literal("hello"))["ts"] == "1.1"
    client.chat_postMessage.side_effect = RuntimeError("channel_not_found")
    with pytest.raises(RuntimeError, match="channel_not_found"):
        await ch.send_literal("hello")


async def test_empty_text_is_refused():
    ch, client = _mocked_channel()
    with pytest.raises(ValueError):
        await ch.send_literal("")
    client.chat_postMessage.assert_not_awaited()

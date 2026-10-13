"""A Slack late answer is posted, never posted-then-edited (2026-10-13 incident).

The late-answer header carries the origin permalink. Slack unfurls it with its own
asynchronous write to the message, and a chat.update landing in that window returned ok
and was then overwritten: the feed kept only the header (4 of 27 late answers in one DM).
The fake client below models that Slack behaviour — an edit made right after the post is
lost — so the test fails on any design that delivers the body through chat.update.
"""
from typing import Any, Dict, List

from src.adapters.slack.response_channel import SLACK_CHUNK_SIZE, SlackResponseChannel


class _UnfurlRacingSlack:
    """Records what the channel ends up showing; an edit is overwritten by the unfurl."""

    def __init__(self) -> None:
        self.feed: Dict[str, Dict[str, Any]] = {}
        self._n = 0

    async def chat_getPermalink(self, **kw: Any) -> Dict[str, Any]:
        return {"ok": True, "permalink": "https://ws.slack.com/archives/D1/p1001"}

    async def chat_postMessage(self, **kw: Any) -> Dict[str, Any]:
        self._n += 1
        ts = f"2000.{self._n:06d}"
        self.feed[ts] = {"text": kw["text"], "thread_ts": kw.get("thread_ts")}
        return {"ok": True, "ts": ts, "channel": kw["channel"]}

    async def chat_update(self, **kw: Any) -> Dict[str, Any]:
        # ok=true, but the pending permalink unfurl rewrites the message from its
        # pre-edit snapshot, so the visible text stays what was posted.
        return {"ok": True}

    def shown(self) -> List[str]:
        return [m["text"] for _, m in sorted(self.feed.items())]


async def test_late_answer_body_survives_the_permalink_unfurl():
    slack = _UnfurlRacingSlack()
    ch = SlackResponseChannel(slack, "D1", "xoxb")

    await ch.send_late_answer(
        "📂 Знайшов *два фото* у `Index test/Photos/`.", "[відкладена відповідь]", "1001.1")

    shown = slack.shown()
    assert len(shown) == 2  # the header, then the body — both posted, neither edited
    assert shown[0].startswith("[відкладена відповідь] <https://ws.slack.com/archives/D1/p1001|↩>")
    assert "Знайшов *два фото*" in shown[1]


async def test_long_late_answer_is_shown_whole_in_the_main_feed():
    slack = _UnfurlRacingSlack()
    ch = SlackResponseChannel(slack, "D1", "xoxb")
    paragraphs = [f"paragraph {i} " + "x" * 600 for i in range(6)]

    await ch.send_late_answer("\n\n".join(paragraphs), "[late answer]", "1001.1")

    assert len(slack.feed) > 2  # the header, then a body that really overflowed past SLACK_CHUNK_SIZE
    assert all(len(t) <= SLACK_CHUNK_SIZE for t in slack.shown())
    assert all(m["thread_ts"] is None for m in slack.feed.values())
    shown = "\n".join(slack.shown())
    assert slack.shown()[0].startswith("[late answer]")
    for p in paragraphs:
        assert p in shown

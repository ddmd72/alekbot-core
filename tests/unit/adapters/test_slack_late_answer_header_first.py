"""A Slack late answer posts its header alone, first (owner request 2026-10-13).

Slack attaches the origin permalink's unfurl preview to the message that carries the link.
With the header inside the first chunk, the preview sat between the first and second chunk
of a long answer; posted alone and first, it sits above the whole answer.
"""
from typing import Any, Dict, List

from src.adapters.slack.response_channel import SLACK_CHUNK_SIZE, SlackResponseChannel

_PERMALINK = "https://ws.slack.com/archives/D1/p1001"


class _RecordingSlack:
    def __init__(self) -> None:
        self.posts: List[Dict[str, Any]] = []
        self.updates: List[Dict[str, Any]] = []

    async def chat_getPermalink(self, **kw: Any) -> Dict[str, Any]:
        return {"ok": True, "permalink": _PERMALINK}

    async def chat_postMessage(self, **kw: Any) -> Dict[str, Any]:
        self.posts.append(kw)
        return {"ok": True, "ts": f"2000.{len(self.posts):06d}", "channel": kw["channel"]}

    async def chat_update(self, **kw: Any) -> Dict[str, Any]:
        self.updates.append(kw)
        return {"ok": True}


async def test_the_header_with_the_permalink_is_the_first_post_and_holds_nothing_else():
    slack = _RecordingSlack()
    ch = SlackResponseChannel(slack, "D1", "xoxb")

    await ch.send_late_answer("the answer body", "[late answer]", "1001.1")

    assert [p["text"] for p in slack.posts] == [f"[late answer] <{_PERMALINK}|↩>", "the answer body"]
    assert all(p.get("thread_ts") is None for p in slack.posts)
    assert slack.updates == []


async def test_only_the_header_carries_the_permalink_when_the_answer_overflows():
    slack = _RecordingSlack()
    ch = SlackResponseChannel(slack, "D1", "xoxb")
    body = "\n\n".join(f"paragraph {i} " + "x" * 600 for i in range(6))

    await ch.send_late_answer(body, "[late answer]", "1001.1")

    texts = [p["text"] for p in slack.posts]
    assert len(texts) >= 3  # the header, then at least two body chunks
    assert texts[0] == f"[late answer] <{_PERMALINK}|↩>"
    assert all(_PERMALINK not in t for t in texts[1:])
    assert all(len(t) <= SLACK_CHUNK_SIZE for t in texts)
    assert sum(t.count("x") for t in texts[1:]) == 6 * 600
    assert all(p.get("thread_ts") is None for p in slack.posts)
    assert slack.updates == []


async def test_an_empty_body_posts_the_header_only():
    slack = _RecordingSlack()
    ch = SlackResponseChannel(slack, "D1", "xoxb")

    await ch.send_late_answer("  ", "[late answer]", "1001.1")

    assert [p["text"] for p in slack.posts] == [f"[late answer] <{_PERMALINK}|↩>"]

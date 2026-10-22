"""POST /slack/events through the Quart test client (P7 finding F4: the route's 401 never ran in a test; only
`_verify_signature` was called directly). `src/adapters/slack/http_adapter.py:158-167`, `:582-623`.

The signing secret and the HMAC are real; the queue records what was enqueued; nothing behind the gate is reachable
(Tripwire). A refused event enqueues nothing.
"""
import hashlib
import hmac
import json
import time

import pytest
from quart import Quart

from src.adapters.slack.http_adapter import HTTPModeAdapter
from tests.unit.web.gate_fakes import RecordingTaskQueue, Tripwire

SIGNING_SECRET = "p7-slack-signing-secret"
EVENT = json.dumps({"type": "event_callback", "event_id": "Ev1", "team_id": "T1",
                    "event": {"type": "message", "channel": "C1", "user": "U1", "text": "hi", "ts": "1.1"}})


def _sign(body: bytes, timestamp: str = None) -> dict:
    ts = str(int(time.time())) if timestamp is None else timestamp
    sig = "v0=" + hmac.new(SIGNING_SECRET.encode(), f"v0:{ts}:{body.decode('utf-8', 'replace')}".encode(),
                           hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig, "Content-Type": "application/json"}


class World:
    def __init__(self) -> None:
        self.touched: list = []
        self.queue = RecordingTaskQueue()
        adapter = HTTPModeAdapter(
            app=Tripwire("bolt_app", self.touched),
            config={"SLACK_SIGNING_SECRET": SIGNING_SECRET, "SLACK_BOT_TOKEN": "xoxb-test"},
            task_service=self.queue,
            session_store=Tripwire("session_store", self.touched),
            conversation_handler=Tripwire("conversation_handler", self.touched),
            iam_service=Tripwire("iam_service", self.touched),
        )
        app = Quart("slack_route")
        app.register_blueprint(adapter.get_blueprint(), url_prefix="/slack")   # as main.py registers it
        self.client = app.test_client()

    def nothing_happened(self) -> bool:
        return self.queue.tasks == [] and self.touched == []

    async def post(self, body: bytes, headers: dict):
        return await self.client.post("/slack/events", data=body, headers=headers)


async def test_a_correctly_signed_event_is_enqueued():
    body = EVENT.encode()
    w = World()
    resp = await w.post(body, _sign(body))
    assert resp.status_code == 200
    assert [t["task_type"] for t in w.queue.tasks] == ["slack_event"]


async def test_no_signature_headers_is_401_and_nothing_is_enqueued():
    w = World()
    resp = await w.post(EVENT.encode(), {"Content-Type": "application/json"})
    assert resp.status_code == 401
    assert w.nothing_happened()


@pytest.mark.parametrize("drop", ["X-Slack-Request-Timestamp", "X-Slack-Signature"])
async def test_one_missing_signature_header_is_401(drop):
    body = EVENT.encode()
    headers = _sign(body)
    del headers[drop]
    w = World()
    resp = await w.post(body, headers)
    assert resp.status_code == 401
    assert w.nothing_happened()


async def test_bad_signature_is_401():
    body = EVENT.encode()
    headers = _sign(body)
    headers["X-Slack-Signature"] = "v0=" + "0" * 64
    w = World()
    resp = await w.post(body, headers)
    assert resp.status_code == 401
    assert w.nothing_happened()


async def test_stale_timestamp_is_401_even_when_correctly_signed():
    body = EVENT.encode()
    w = World()
    resp = await w.post(body, _sign(body, timestamp=str(int(time.time()) - 301)))
    assert resp.status_code == 401
    assert w.nothing_happened()


async def test_non_integer_timestamp_is_401():
    body = EVENT.encode()
    w = World()
    resp = await w.post(body, _sign(body, timestamp="soon"))
    assert resp.status_code == 401
    assert w.nothing_happened()


async def test_a_body_that_is_json_but_not_utf8_is_401():
    # json.loads accepts UTF-16; the signature check then fails to decode it and refuses (http_adapter.py:621-623).
    body = EVENT.encode("utf-16")
    w = World()
    resp = await w.post(body, _sign(EVENT.encode()))
    assert resp.status_code == 401
    assert w.nothing_happened()


async def test_url_verification_is_the_only_unsigned_type_answered():
    w = World()
    resp = await w.post(json.dumps({"type": "url_verification", "challenge": "c-123"}).encode(),
                        {"Content-Type": "application/json"})
    assert resp.status_code == 200
    assert await resp.get_data(as_text=True) == "c-123"
    assert resp.headers["Content-Type"].startswith("text/plain")
    assert w.nothing_happened()


@pytest.mark.parametrize("event_type", ["event_callback", "app_rate_limited", "block_actions", "", None])
async def test_every_other_type_needs_a_signature(event_type):
    payload = json.loads(EVENT)
    payload["type"] = event_type
    w = World()
    resp = await w.post(json.dumps(payload).encode(), {"Content-Type": "application/json"})
    assert resp.status_code == 401
    assert w.nothing_happened()

"""POST /slack/events and /slack/interactivity: the signature is checked on the raw bytes BEFORE anything is parsed, for
every request type, and the body is read with a size cap (S4 review R2). Through the Quart test client with a real
signing secret and HMAC; nothing behind the gate is reachable (Tripwire)."""
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from quart import Quart

from src.adapters.slack.http_adapter import HTTPModeAdapter
from src.adapters.slack.request_body import MAX_BODY_BYTES
from tests.unit.web.gate_fakes import RecordingTaskQueue, Tripwire

SECRET = "r2-slack-signing-secret"
JSON_HEADERS = {"Content-Type": "application/json"}


def _signed(body: bytes, ts: str = None) -> dict:
    ts = str(int(time.time())) if ts is None else ts
    digest = hmac.new(SECRET.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": "v0=" + digest, **JSON_HEADERS}


class World:
    def __init__(self) -> None:
        self.touched: list = []
        self.queue = RecordingTaskQueue()
        adapter = HTTPModeAdapter(
            app=Tripwire("bolt_app", self.touched),
            config={"SLACK_SIGNING_SECRET": SECRET, "SLACK_BOT_TOKEN": "xoxb-test"},
            task_service=self.queue,
            session_store=Tripwire("session_store", self.touched),
            conversation_handler=Tripwire("conversation_handler", self.touched),
            iam_service=Tripwire("iam_service", self.touched),
        )
        app = Quart("slack_route_order")
        app.register_blueprint(adapter.get_blueprint(), url_prefix="/slack")
        self.client = app.test_client()

    async def events(self, body: bytes, headers: dict):
        return await self.client.post("/slack/events", data=body, headers=headers)


async def test_a_forged_url_verification_gets_no_challenge():
    body = json.dumps({"type": "url_verification", "challenge": "c-forged"}).encode()
    resp = await World().events(body, JSON_HEADERS)
    assert resp.status_code == 401
    assert "c-forged" not in await resp.get_data(as_text=True)


async def test_a_url_verification_with_a_wrong_signature_gets_no_challenge():
    body = json.dumps({"type": "url_verification", "challenge": "c-forged"}).encode()
    headers = _signed(body)
    headers["X-Slack-Signature"] = "v0=" + "0" * 64
    resp = await World().events(body, headers)
    assert resp.status_code == 401


async def test_a_signed_url_verification_is_answered_with_its_challenge():
    body = json.dumps({"type": "url_verification", "challenge": "c-123"}).encode()
    w = World()
    resp = await w.events(body, _signed(body))
    assert resp.status_code == 200
    assert await resp.get_data(as_text=True) == "c-123"
    assert resp.headers["Content-Type"].startswith("text/plain")
    assert w.queue.tasks == [] and w.touched == []


@pytest.mark.parametrize("raw", [b"[1, 2]", b'"text"', b"42", b"null", b"true"])
async def test_a_signed_body_that_is_json_but_not_an_object_is_400(raw):
    w = World()
    resp = await w.events(raw, _signed(raw))
    assert resp.status_code == 400
    assert w.queue.tasks == [] and w.touched == []


@pytest.mark.parametrize("raw", [b"not json at all", b"", b"\xff\xfe\x00"])
async def test_a_signed_body_that_is_not_json_is_400(raw):
    w = World()
    resp = await w.events(raw, _signed(raw))
    assert resp.status_code == 400
    assert w.queue.tasks == [] and w.touched == []


@pytest.mark.parametrize("raw", [b"[1, 2]", b"not json", b'{"type": "event_callback"'])
async def test_an_unsigned_body_is_refused_before_it_is_parsed(raw):
    """Whatever the body is, no signature is 401: a 400 would show the body was parsed first."""
    resp = await World().events(raw, JSON_HEADERS)
    assert resp.status_code == 401


async def test_a_signed_body_over_the_cap_is_413_and_nothing_is_enqueued():
    body = b'{"type": "event_callback", "pad": "' + b"x" * MAX_BODY_BYTES + b'"}'
    w = World()
    resp = await w.events(body, _signed(body))
    assert resp.status_code == 413
    assert w.queue.tasks == [] and w.touched == []


async def test_a_body_without_a_content_length_is_counted_as_it_arrives():
    w = World()
    async with w.client.request("/slack/events", method="POST", headers=JSON_HEADERS) as connection:
        chunk = b"x" * 65536
        for _ in range(MAX_BODY_BYTES // len(chunk) + 2):
            await connection.send(chunk)
        await connection.send_complete()
    resp = await connection.as_response()
    assert resp.status_code == 413
    assert w.queue.tasks == [] and w.touched == []


async def test_a_body_at_the_cap_is_still_read():
    pad = "x" * (MAX_BODY_BYTES - 200)
    body = json.dumps({"type": "event_callback", "event_id": "Ev1", "pad": pad,
                       "event": {"type": "message", "channel": "C1", "user": "U1", "text": "hi", "ts": "1.1"}}).encode()
    assert len(body) <= MAX_BODY_BYTES
    w = World()
    resp = await w.events(body, _signed(body))
    assert resp.status_code == 200
    assert [t["task_type"] for t in w.queue.tasks] == ["slack_event"]


async def test_the_interactivity_route_refuses_a_body_over_the_cap():
    body = urlencode({"payload": "x" * MAX_BODY_BYTES}).encode()
    w = World()
    resp = await w.client.post("/slack/interactivity", data=body, headers=_signed(body))
    assert resp.status_code == 413
    assert w.queue.tasks == [] and w.touched == []


async def test_the_interactivity_route_still_checks_the_signature_first():
    body = urlencode({"payload": json.dumps({"type": "block_actions"})}).encode()
    resp = await World().client.post("/slack/interactivity", data=body, headers=JSON_HEADERS)
    assert resp.status_code == 401

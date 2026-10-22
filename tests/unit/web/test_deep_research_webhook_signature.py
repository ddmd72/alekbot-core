"""The OpenAI deep-research webhook WITH its secret configured (P7 finding F2: every earlier test built the blueprint
without one, so the Svix HMAC check never ran; `src/web/deep_research_webhooks.py:78-138`).

A refused delivery must claim nothing and enqueue nothing. The accepted signature is built here from the Svix
recipe and also checked by the OpenAI SDK's own verifier, so the format the route accepts is the one OpenAI signs.
"""
import base64
import hashlib
import hmac
import json
import time

import pytest
from openai.resources.webhooks import Webhooks
from quart import Quart

from src.adapters.firestore_dedup_store import FirestoreDedupStore
from src.web.deep_research_webhooks import create_deep_research_webhooks_blueprint
from tests.support.firestore_eval import EvalDb
from tests.unit.web.gate_fakes import RecordingTaskQueue

_KEY = b"p7-deep-research-webhook-key-32b"
SECRET = "whsec_" + base64.b64encode(_KEY).decode()
PATH = "/webhooks/openai/deep-research"
BODY = json.dumps({"type": "response.completed", "data": {"id": "resp_1", "metadata": {
    "user_id": "u1", "account_id": "a1", "query": "tides", "session_id": "u1:C9", "origin_platform": "slack"}}})


def _sign(body: str, webhook_id: str = "msg_1", timestamp: int = None) -> dict:
    ts = str(int(time.time()) if timestamp is None else timestamp)
    digest = hmac.new(_KEY, f"{webhook_id}.{ts}.{body}".encode(), hashlib.sha256).digest()
    return {"webhook-id": webhook_id, "webhook-timestamp": ts,
            "webhook-signature": "v1," + base64.b64encode(digest).decode(), "Content-Type": "application/json"}


class World:
    def __init__(self) -> None:
        self.db = EvalDb()
        self.queue = RecordingTaskQueue()
        app = Quart("dr_webhook")
        app.register_blueprint(create_deep_research_webhooks_blueprint(
            webhook_secret=SECRET, task_queue=self.queue, dedup_store=FirestoreDedupStore(self.db, "dedup")))
        self.client = app.test_client()

    def nothing_happened(self) -> bool:
        return self.queue.tasks == [] and self.db.collection("dedup").docs == {}

    async def post(self, body: str, headers: dict):
        return await self.client.post(PATH, data=body, headers=headers)


async def test_a_signature_built_as_openai_signs_is_accepted_and_enqueued():
    headers = _sign(BODY)
    # The SDK's own verifier accepts these exact headers: the format is OpenAI's, not ours. (Its `self` is only read
    # when no secret is passed, so no client - and no TLS context - is built for it.)
    Webhooks.__new__(Webhooks).verify_signature(BODY, headers, secret=SECRET)
    w = World()
    resp = await w.post(BODY, headers)
    assert resp.status_code == 200
    assert [t["task_type"] for t in w.queue.tasks] == ["deep_research_webhook_event"]
    assert list(w.db.collection("dedup").docs) == ["openai_deep_research:resp_1"]


async def test_bad_signature_is_401_and_nothing_is_claimed_or_enqueued():
    headers = _sign(BODY)
    headers["webhook-signature"] = "v1," + base64.b64encode(b"\0" * 32).decode()
    w = World()
    resp = await w.post(BODY, headers)
    assert resp.status_code == 401
    assert w.nothing_happened()


async def test_signature_over_another_body_is_401():
    w = World()
    resp = await w.post(BODY, _sign(BODY.replace("resp_1", "resp_2")))
    assert resp.status_code == 401
    assert w.nothing_happened()


@pytest.mark.parametrize("missing", ["webhook-id", "webhook-timestamp", "webhook-signature"])
async def test_each_missing_svix_header_is_401(missing):
    headers = _sign(BODY)
    del headers[missing]
    w = World()
    resp = await w.post(BODY, headers)
    assert resp.status_code == 401
    assert w.nothing_happened()


async def test_non_integer_timestamp_is_401():
    headers = _sign(BODY)
    headers["webhook-timestamp"] = "yesterday"
    w = World()
    resp = await w.post(BODY, headers)
    assert resp.status_code == 401
    assert w.nothing_happened()


@pytest.mark.parametrize("age_s", [301, -301])
async def test_timestamp_outside_the_300_s_window_is_401_even_when_correctly_signed(age_s):
    w = World()
    resp = await w.post(BODY, _sign(BODY, timestamp=int(time.time()) - age_s))
    assert resp.status_code == 401
    assert w.nothing_happened()


async def test_invalid_json_after_a_valid_signature_is_400_and_nothing_is_enqueued():
    body = "{not json"
    w = World()
    resp = await w.post(body, _sign(body))
    assert resp.status_code == 400
    assert w.nothing_happened()

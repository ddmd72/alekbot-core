"""The OpenAI deep-research webhook: a secret without the `whsec_` prefix, and a claim that cannot be released
(P7, C1: `src/web/deep_research_webhooks.py` held at 100 % lines)."""
import base64
import hashlib
import hmac
import json
import time

from google.api_core.exceptions import ServiceUnavailable
from quart import Quart

from src.adapters.firestore_dedup_store import FirestoreDedupStore
from src.web.deep_research_webhooks import create_deep_research_webhooks_blueprint
from tests.support.firestore_eval import EvalDb
from tests.unit.web.gate_fakes import RecordingTaskQueue

PATH = "/webhooks/openai/deep-research"
BODY = json.dumps({"type": "response.completed", "data": {"id": "resp_1", "metadata": {"user_id": "u1"}}})


def _sign(key: bytes, body: str) -> dict:
    ts = str(int(time.time()))
    digest = hmac.new(key, f"msg_1.{ts}.{body}".encode(), hashlib.sha256).digest()
    return {"webhook-id": "msg_1", "webhook-timestamp": ts,
            "webhook-signature": "v1," + base64.b64encode(digest).decode(), "Content-Type": "application/json"}


def _client(secret: str, queue, dedup):
    app = Quart("dr_webhook_c1")
    app.register_blueprint(create_deep_research_webhooks_blueprint(webhook_secret=secret, task_queue=queue,
                                                                   dedup_store=dedup))
    return app.test_client()


async def test_a_secret_without_the_whsec_prefix_is_used_as_raw_bytes():
    queue, db = RecordingTaskQueue(), EvalDb()
    client = _client("plain-shared-secret", queue, FirestoreDedupStore(db, "dedup"))
    accepted = await client.post(PATH, data=BODY, headers=_sign(b"plain-shared-secret", BODY))
    refused = await client.post(PATH, data=BODY, headers=_sign(b"another-secret", BODY))
    assert accepted.status_code == 200 and refused.status_code == 401
    assert len(queue.tasks) == 1


class _QueueDown(RecordingTaskQueue):
    async def enqueue_worker_task(self, task_type=None, payload=None, **kwargs):
        raise ServiceUnavailable("cloud tasks unavailable")


class _ReleaseFails(FirestoreDedupStore):
    async def release(self, event_id):
        raise ServiceUnavailable("firestore unavailable")


async def test_enqueue_failure_whose_claim_cannot_be_released_still_answers_500():
    # 500 makes OpenAI redeliver; the claim that stayed behind expires with its TTL (the mark's own expiry).
    db = EvalDb()
    client = _client("plain-shared-secret", _QueueDown(), _ReleaseFails(db, "dedup"))
    resp = await client.post(PATH, data=BODY, headers=_sign(b"plain-shared-secret", BODY))
    assert resp.status_code == 500
    assert (await resp.get_json()) == {"error": "enqueue failed"}
    assert list(db.collection("dedup").docs) == ["openai_deep_research:resp_1"]

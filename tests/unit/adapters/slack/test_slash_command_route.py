"""POST /slack/commands through the Quart test client: the signature is checked on the raw bytes before the form is
parsed; a signed command is queued (never run inline) and answered with an empty 200."""
import hashlib
import hmac
import time
from urllib.parse import urlencode

from quart import Quart

from src.adapters.slack.http_adapter import HTTPModeAdapter
from tests.unit.web.gate_fakes import RecordingTaskQueue, Tripwire

SIGNING_SECRET = "slash-signing-secret"


def _sign(body: bytes) -> dict:
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(SIGNING_SECRET.encode(), f"v0:{ts}:{body.decode()}".encode(), hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig,
            "Content-Type": "application/x-www-form-urlencoded"}


def _form(**fields) -> bytes:
    return urlencode(fields).encode()


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
        app = Quart("slack_commands")
        app.register_blueprint(adapter.get_blueprint(), url_prefix="/slack")
        self.client = app.test_client()

    async def post(self, body: bytes, headers: dict):
        return await self.client.post("/slack/commands", data=body, headers=headers)


async def test_a_bad_signature_is_401_and_nothing_is_enqueued():
    body = _form(command="/geo", text="here", user_id="U0SLACK", channel_id="C0CHAN")
    headers = _sign(body)
    headers["X-Slack-Signature"] = "v0=" + "0" * 64
    w = World()
    resp = await w.post(body, headers)
    assert resp.status_code == 401
    assert w.queue.tasks == []


async def test_an_unsigned_command_is_401():
    w = World()
    resp = await w.post(_form(command="/geo", user_id="U0SLACK"), {"Content-Type": "application/x-www-form-urlencoded"})
    assert resp.status_code == 401
    assert w.queue.tasks == []


async def test_a_signed_body_without_a_command_is_400():
    body = _form(text="here", user_id="U0SLACK")
    w = World()
    resp = await w.post(body, _sign(body))
    assert resp.status_code == 400
    assert w.queue.tasks == []


async def test_a_command_not_starting_with_a_slash_is_400():
    body = _form(command="geo", user_id="U0SLACK")
    w = World()
    resp = await w.post(body, _sign(body))
    assert resp.status_code == 400
    assert w.queue.tasks == []


async def test_a_signed_body_without_a_user_is_400():
    body = _form(command="/geo", text="here")
    w = World()
    resp = await w.post(body, _sign(body))
    assert resp.status_code == 400
    assert w.queue.tasks == []


async def test_a_valid_command_is_queued_and_answered_with_an_empty_200():
    body = _form(command="/geo", text="look here", user_id="U0SLACK", channel_id="C0CHAN", trigger_id="T0TRIG")
    w = World()
    resp = await w.post(body, _sign(body))
    assert resp.status_code == 200
    assert await resp.get_data() == b""
    assert len(w.queue.tasks) == 1
    task = w.queue.tasks[0]
    assert task["task_type"] == "slack_event"
    assert task["session_id"] == "U0SLACK"
    assert task["task_name"] is None
    assert task["event_data"] == {"event": {"type": "slash_command", "command": "/geo", "text": "look here",
                                            "user": "U0SLACK", "channel": "C0CHAN"}}
    assert w.touched == []


async def test_a_command_without_text_is_queued_with_empty_text():
    body = _form(command="/memory", user_id="U0SLACK", channel_id="C0CHAN")
    w = World()
    resp = await w.post(body, _sign(body))
    assert resp.status_code == 200
    assert w.queue.tasks[0]["event_data"]["event"]["text"] == ""


async def test_a_refused_queue_is_500():
    body = _form(command="/geo", user_id="U0SLACK")
    w = World()

    async def refuse(**kwargs):
        raise RuntimeError("queue down")

    w.queue.enqueue_slack_event = refuse
    resp = await w.post(body, _sign(body))
    assert resp.status_code == 500
    assert await resp.get_data() == b""


async def test_an_oversize_body_is_413_and_nothing_is_enqueued():
    body = b"command=%2Fgeo&user_id=U0SLACK&text=" + b"a" * (1024 * 1024 + 1)
    w = World()
    resp = await w.post(body, _sign(body))
    assert resp.status_code == 413
    assert w.queue.tasks == []

"""`POST /slack/interactivity` (Facts S4 · T5d, plan §3.20 "Inbound") and `verify_slack_signature`.

Through the Quart test client on the real `HTTPModeAdapter` blueprint, registered as `main.py` registers it, with the real
`GcpTaskQueue` over a Cloud Tasks client that refuses a task name it has seen. The signing secret and every HMAC are real,
computed over the raw body; the dependencies behind the gate are tripwires. A refused request touches and enqueues
nothing; a signed one is answered at once and leaves one task. The last class carries a press through the queue and the
worker onto a real memory-review question."""
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

import pytest
from quart import Quart

from unittest.mock import AsyncMock, MagicMock

from src.adapters.slack.binding_codec import encode
from src.adapters.slack.http_adapter import HTTPModeAdapter
from src.adapters.slack.interactive_message_adapter import SlackInteractiveMessageAdapter
from src.adapters.slack.interactivity_route import SlackInteractivityRoute
from src.adapters.slack.signature import MAX_AGE_S, verify_slack_signature
from src.domain.auth import IAMDecision
from src.domain.fact_review_language import LABEL_KEYS, fact_question_message
from src.domain.interactive_binding import InteractiveBinding
from src.domain.interactive_choice import INTERACTIVE_CHOICE_TASK, InteractiveChoice
from src.domain.messaging import MessageRef
from src.domain.user import UserProfile
from src.handlers.worker_handler import WorkerHandler
from src.ports.platform_auth_port import PlatformAuthPort
from src.services.interactive_choice_service import InteractiveChoiceService
from tests.support.fact_kit import ACC
from tests.support.fact_review_world import ReviewWorld
from tests.support.recording_task_queue import make_task_queue
from tests.support.review_rule_builders import question
from tests.unit.services.test_fact_question_choice import USER, question_on, ref_of, service, stale_fact
from tests.unit.web.gate_fakes import Tripwire

SECRET = "t5d-slack-signing-secret"
BLOCK_ID = "fact_question:q-1"
ACTION_TS = "1761296000.123456"
PRESSED_AT = datetime.fromtimestamp(float(ACTION_TS), tz=timezone.utc)
MESSAGE_TS = "1761295900.000200"


def sign(body: bytes, secret: str = SECRET, timestamp: Optional[str] = None) -> Dict[str, str]:
    ts = str(int(time.time())) if timestamp is None else timestamp
    digest = hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": "v0=" + digest,
            "Content-Type": "application/x-www-form-urlencoded"}


def form(payload: Any) -> bytes:
    return urlencode({"payload": payload if isinstance(payload, str) else json.dumps(payload)}).encode()


def press(**over: Any) -> Dict[str, Any]:
    """What Slack sends for one press of one of our buttons."""
    action = {"type": "button", "block_id": BLOCK_ID, "action_id": "ended", "value": "ended", "action_ts": ACTION_TS}
    payload: Dict[str, Any] = {
        "type": "block_actions", "user": {"id": "U0OWNER", "team_id": "T1"},
        "container": {"type": "message", "message_ts": MESSAGE_TS, "channel_id": "D1"},
        "channel": {"id": "D1"}, "message": {"ts": MESSAGE_TS}, "actions": [action],
    }
    payload.update(over)
    return payload


def expected_choice(**over: Any) -> InteractiveChoice:
    fields: Dict[str, Any] = dict(
        binding=InteractiveBinding(kind="fact_question", id="q-1"), option_id="ended", platform="slack",
        platform_user_id="U0OWNER", message_ref=MessageRef("slack", "D1", MESSAGE_TS), at=PRESSED_AT)
    fields.update(over)
    return InteractiveChoice(**fields)


class World:
    def __init__(self) -> None:
        self.touched: List[str] = []
        self.queue, self.tasks_client = make_task_queue()
        adapter = HTTPModeAdapter(
            app=Tripwire("bolt_app", self.touched),
            config={"SLACK_SIGNING_SECRET": SECRET, "SLACK_BOT_TOKEN": "xoxb-test"},
            task_service=Tripwire("events_queue", self.touched),
            session_store=Tripwire("session_store", self.touched),
            conversation_handler=Tripwire("conversation_handler", self.touched),
            iam_service=Tripwire("iam_service", self.touched),
            worker_queue=self.queue,
        )
        app = Quart("slack_interactivity")
        app.register_blueprint(adapter.get_blueprint(), url_prefix="/slack")      # as main.py registers it
        self.http = app.test_client()

    @property
    def tasks(self) -> List[Dict[str, Any]]:
        return [body for _, body in self.tasks_client.worker_tasks()]

    def nothing_happened(self) -> bool:
        return self.tasks == [] and self.touched == []

    async def post(self, body: bytes, headers: Dict[str, str]):
        return await self.http.post("/slack/interactivity", data=body, headers=headers)

    async def press(self, payload: Optional[Dict[str, Any]] = None):
        body = form(press() if payload is None else payload)
        return await self.post(body, sign(body))


# ------------------------------------------------------------------------------------------------ a signed press

class TestSignedPress:
    async def test_a_signed_press_leaves_one_task_with_the_choice_and_is_answered_200(self):
        world = World()

        resp = await world.press()

        assert resp.status_code == 200 and await resp.get_json() == {"ok": True}
        [task] = world.tasks
        assert task["task_type"] == INTERACTIVE_CHOICE_TASK == "interactive_choice"
        assert InteractiveChoice.from_payload(task) == expected_choice()
        assert world.touched == []

    async def test_the_same_press_twice_is_one_task_and_both_are_200(self):
        world = World()

        first = await world.press()
        second = await world.press()                    # Slack's own retry: same action_ts, user and block

        assert (first.status_code, second.status_code) == (200, 200)
        assert len(world.tasks) == 1

    async def test_two_presses_are_two_tasks(self):
        world = World()
        other = press()
        other["actions"][0]["action_ts"] = "1761296001.000001"

        await world.press()
        await world.press(other)

        assert len(world.tasks) == 2

    async def test_the_press_of_another_user_on_the_same_button_is_its_own_task(self):
        world = World()
        other = press(user={"id": "U0OTHER"})

        await world.press()
        await world.press(other)

        assert [t["platform_user_id"] for t in world.tasks] == ["U0OWNER", "U0OTHER"]

    async def test_the_press_of_the_posters_own_button_decodes_to_its_binding_and_option(self):
        class Client:
            def __init__(self) -> None:
                self.blocks: List[dict] = []

            async def chat_postMessage(self, **kwargs):
                self.blocks = kwargs["blocks"]
                return {"channel": "D1", "ts": MESSAGE_TS}

        client = Client()
        message = fact_question_message(question(question_id="q-77"), {key: key for key in LABEL_KEYS})
        await SlackInteractiveMessageAdapter(client).post("slack", "D1", message)
        [actions] = [b for b in client.blocks if b["type"] == "actions"]
        button = actions["elements"][3]
        payload = press(actions=[{"type": "button", "block_id": actions["block_id"], "action_id": button["action_id"],
                                  "action_ts": ACTION_TS}])
        world = World()

        await world.press(payload)

        [task] = world.tasks
        assert (task["binding"], task["option_id"]) == ({"kind": "fact_question", "id": "q-77"}, "dont_know")

    async def test_the_channel_and_message_come_from_the_container_when_the_payload_has_no_message_section(self):
        world = World()
        payload = press()
        del payload["channel"], payload["message"]

        await world.press(payload)

        [task] = world.tasks
        assert task["message_ref"] == {"platform": "slack", "channel_id": "D1", "message_id": MESSAGE_TS}

    async def test_the_message_section_wins_over_the_container(self):
        world = World()
        payload = press(channel={"id": "D9"}, message={"ts": "1761295999.000900"})

        await world.press(payload)

        [task] = world.tasks
        assert task["message_ref"] == {"platform": "slack", "channel_id": "D9", "message_id": "1761295999.000900"}

    async def test_the_first_action_of_ours_is_the_choice_among_foreign_ones(self):
        world = World()
        payload = press()
        payload["actions"] = [{"block_id": "someone-elses-block", "action_id": "x"}, "junk", payload["actions"][0]]

        await world.press(payload)

        [task] = world.tasks
        assert task["option_id"] == "ended"


# ------------------------------------------------------------------------------------------------ not ours: ignored

class TestIgnored:
    @pytest.mark.parametrize("payload", [
        {"type": "view_submission", "user": {"id": "U0OWNER"}, "view": {"callback_id": "x"}},
        {"type": "shortcut", "user": {"id": "U0OWNER"}},
        {"user": {"id": "U0OWNER"}},
        {"type": ["block_actions"]},
    ])
    async def test_a_signed_payload_that_is_not_block_actions_is_200_and_nothing_happens(self, payload):
        world = World()

        resp = await world.press(payload)

        assert resp.status_code == 200 and world.nothing_happened()

    @pytest.mark.parametrize("block_id", ["", "no-colon", "Bad Kind:1", "fact_question:", "fact_question:has space",
                                          "fact_question:" + "x" * 200, 5, None])
    async def test_a_signed_action_whose_block_id_does_not_parse_is_200_and_nothing_happens(self, block_id):
        world = World()
        payload = press()
        payload["actions"][0]["block_id"] = block_id

        resp = await world.press(payload)

        assert resp.status_code == 200 and world.nothing_happened()

    @pytest.mark.parametrize("actions", [None, [], "buttons", {"block_id": BLOCK_ID}, ["junk", 3]])
    async def test_a_signed_block_actions_without_a_usable_action_list_is_200_and_nothing_happens(self, actions):
        world = World()
        payload = press()
        payload["actions"] = actions

        resp = await world.press(payload)

        assert resp.status_code == 200 and world.nothing_happened()

    @pytest.mark.parametrize("damage", [
        {"user": {}}, {"user": "U0OWNER"}, {"user": None},
        {"container": {}, "channel": {}, "message": {}},
        {"container": "x", "channel": [], "message": 3},
    ])
    async def test_a_press_without_a_user_or_a_message_is_200_and_nothing_happens(self, damage):
        world = World()

        resp = await world.press(press(**damage))

        assert resp.status_code == 200 and world.nothing_happened()

    async def test_a_press_with_no_option_is_200_and_nothing_happens(self):
        world = World()
        payload = press()
        payload["actions"][0]["action_id"] = ""

        resp = await world.press(payload)

        assert resp.status_code == 200 and world.nothing_happened()


# ------------------------------------------------------------------------------------------------ the press's time and key

class TestTimeAndKey:
    @pytest.mark.parametrize("action_ts", ["", "soon", "1e400", "-1e30"])
    async def test_a_press_without_a_usable_action_time_is_dated_when_the_request_came(self, action_ts):
        clock_now = 1761296500.0
        queue, _ = make_task_queue()
        route = SlackInteractivityRoute(SECRET, queue, clock=lambda: clock_now)
        payload = press()
        payload["actions"][0]["action_ts"] = action_ts
        body = form(payload)

        body_out, status = await route.handle(body, sign(body, timestamp=str(int(clock_now))))

        assert (body_out, status) == ({"ok": True}, 200)
        [(_, task)] = queue.client.worker_tasks()
        assert InteractiveChoice.from_payload(task).at == datetime.fromtimestamp(clock_now, tz=timezone.utc)

    async def test_a_press_without_an_action_time_has_no_dedup_name_so_each_is_a_task(self):
        clock_now = 1761296500.0
        queue, client = make_task_queue()
        route = SlackInteractivityRoute(SECRET, queue, clock=lambda: clock_now)
        payload = press()
        del payload["actions"][0]["action_ts"]
        body = form(payload)
        headers = sign(body, timestamp=str(int(clock_now)))

        await route.handle(body, headers)
        await route.handle(body, headers)

        assert len(client.worker_tasks()) == 2
        assert all("name" not in request["task"] for request in client.create_task_requests)

    async def test_a_press_with_an_action_time_has_a_name_from_it_the_user_and_the_block(self):
        world = World()
        await world.press()

        [request] = world.tasks_client.create_task_requests
        name = request["task"]["name"]
        digest = hashlib.sha256(f"{ACTION_TS}:U0OWNER:{BLOCK_ID}".encode()).hexdigest()[:32]
        assert name.endswith(f"/interactive_choice-{digest}")


# ------------------------------------------------------------------------------------------------ refused

class TestRefused:
    @pytest.mark.parametrize("body", [
        form(press()),                                           # well-formed
        b"payload=%7Bnot-json",                                  # malformed
        b"",
        b"\xff\xfe\x00",
    ])
    async def test_an_unsigned_request_is_401_and_nothing_is_parsed_or_enqueued(self, body, monkeypatch):
        world = World()

        def parsed(*_args, **_kwargs):
            raise AssertionError("an unsigned body was parsed")

        monkeypatch.setattr(SlackInteractivityRoute, "_payload", parsed)
        monkeypatch.setattr(SlackInteractivityRoute, "_choice", parsed)

        resp = await world.post(body, {"Content-Type": "application/x-www-form-urlencoded"})

        assert resp.status_code == 401 and world.nothing_happened()

    @pytest.mark.parametrize("body", [form(press()), b"payload=%7Bnot-json", b"\xff\xfe\x00"])
    async def test_a_badly_signed_request_is_401_and_nothing_is_parsed_or_enqueued(self, body, monkeypatch):
        world = World()

        def parsed(*_args, **_kwargs):
            raise AssertionError("a badly signed body was parsed")

        monkeypatch.setattr(SlackInteractivityRoute, "_payload", parsed)
        headers = sign(body, secret="a-different-secret")

        resp = await world.post(body, headers)

        assert resp.status_code == 401 and world.nothing_happened()

    async def test_a_signature_over_another_body_is_401(self):
        world = World()
        signed = form(press())
        tampered = form(press(user={"id": "U0ATTACKER"}))

        resp = await world.post(tampered, sign(signed))

        assert resp.status_code == 401 and world.nothing_happened()

    @pytest.mark.parametrize("drop", ["X-Slack-Request-Timestamp", "X-Slack-Signature"])
    async def test_one_missing_signature_header_is_401(self, drop):
        world = World()
        body = form(press())
        headers = sign(body)
        del headers[drop]

        resp = await world.post(body, headers)

        assert resp.status_code == 401 and world.nothing_happened()

    @pytest.mark.parametrize("skew", [-(MAX_AGE_S + 5), MAX_AGE_S + 5, -3600, 86400])
    async def test_a_signed_request_outside_the_window_is_401(self, skew):
        world = World()
        body = form(press())

        resp = await world.post(body, sign(body, timestamp=str(int(time.time()) + skew)))

        assert resp.status_code == 401 and world.nothing_happened()

    @pytest.mark.parametrize("timestamp", ["yesterday", "1.5", "9" * 400, "٣"])
    async def test_a_timestamp_that_is_not_a_whole_number_in_the_window_is_401(self, timestamp):
        world = World()
        body = form(press())

        resp = await world.post(body, sign(body, timestamp=timestamp))

        assert resp.status_code == 401 and world.nothing_happened()

    async def test_a_signature_that_is_not_ascii_is_401(self):
        world = World()
        body = form(press())
        headers = sign(body)
        headers["X-Slack-Signature"] = "v0=é" + headers["X-Slack-Signature"][4:]

        resp = await world.post(body, headers)

        assert resp.status_code == 401 and world.nothing_happened()

    async def test_the_request_is_judged_on_the_raw_bytes_not_on_a_re_encoding(self):
        world = World()
        text = json.dumps(press())
        body = urlencode({"payload": text}, quote_via=quote).encode()         # spaces as %20 ...
        reencoded = urlencode({"payload": text}).encode()                    # ... the same payload with spaces as +
        assert reencoded != body
        headers = sign(body)

        assert (await world.post(body, headers)).status_code == 200
        assert (await world.post(reencoded, headers)).status_code == 401

    @pytest.mark.parametrize("payload", ["{not json", "[1, 2]", "\"text\"", "3", "null"])
    async def test_a_signed_payload_that_is_not_a_json_object_is_400(self, payload):
        world = World()
        body = form(payload)

        resp = await world.post(body, sign(body))

        assert resp.status_code == 400 and world.nothing_happened()

    @pytest.mark.parametrize("body", [b"", b"other=1", b"payload", b"\xff\xfe\x00", b"%ff=%ff"])
    async def test_a_signed_body_without_a_payload_field_is_400(self, body):
        world = World()

        resp = await world.post(body, sign(body))

        assert resp.status_code == 400 and world.nothing_happened()

    async def test_without_a_queue_a_signed_press_is_503_and_an_unsigned_one_is_still_401(self):
        route = SlackInteractivityRoute(SECRET, None)
        body = form(press())

        assert (await route.handle(body, sign(body)))[1] == 503
        assert (await route.handle(body, {}))[1] == 401

    async def test_without_a_queue_a_signed_payload_that_is_ignored_is_still_200(self):
        route = SlackInteractivityRoute(SECRET, None)
        body = form({"type": "view_submission"})

        assert await route.handle(body, sign(body)) == ({"ok": True}, 200)

    async def test_a_queue_that_refuses_the_task_is_500_and_says_nothing_of_why(self):
        class Refusing:
            async def enqueue_worker_task(self, *args, **kwargs):
                raise RuntimeError("quota exhausted for project secret-project-1")

        route = SlackInteractivityRoute(SECRET, Refusing())     # type: ignore[arg-type]
        body = form(press())

        assert await route.handle(body, sign(body)) == ({"error": "not queued"}, 500)

    @pytest.mark.parametrize("secret", ["", None])
    async def test_an_empty_signing_secret_refuses_even_a_body_signed_with_it(self, secret):
        queue, client = make_task_queue()
        route = SlackInteractivityRoute(secret, queue)           # type: ignore[arg-type]
        body = form(press())

        assert (await route.handle(body, sign(body, secret=secret or "")))[1] == 401
        assert client.worker_tasks() == []


# ------------------------------------------------------------------------------------------------ what is logged

class TestLogs:
    async def test_no_log_line_holds_a_signature_or_the_secret(self, caplog):
        world = World()
        body = form(press())
        good = sign(body)
        bad = sign(body, secret="a-different-secret")
        stale = sign(body, timestamp=str(int(time.time()) - 4000))
        with caplog.at_level(logging.DEBUG):
            await world.post(body, good)
            await world.post(body, bad)
            await world.post(body, stale)
            await world.post(body, {})
            route = SlackInteractivityRoute("", None)
            await route.handle(body, good)

        for headers in (good, bad, stale):
            digest = headers["X-Slack-Signature"]
            assert digest not in caplog.text and digest[3:] not in caplog.text and digest[3:23] not in caplog.text
        assert SECRET not in caplog.text and "a-different-secret" not in caplog.text
        assert caplog.records


# ------------------------------------------------------------------------------------------------ the signature check

class TestVerifySlackSignature:
    BODY = b"payload=%7B%7D"

    def headers(self, **over: str) -> Dict[str, str]:
        return {**sign(self.BODY, timestamp="1761296000"), **over}

    def test_a_signed_body_in_the_window_is_valid(self):
        assert verify_slack_signature(self.BODY, self.headers(), SECRET, 1761296000.0)

    def test_the_window_is_inclusive_at_its_edges(self):
        assert verify_slack_signature(self.BODY, self.headers(), SECRET, 1761296000.0 + MAX_AGE_S)
        assert verify_slack_signature(self.BODY, self.headers(), SECRET, 1761296000.0 - MAX_AGE_S)
        assert not verify_slack_signature(self.BODY, self.headers(), SECRET, 1761296000.0 + MAX_AGE_S + 0.5)

    def test_header_names_are_matched_without_regard_to_case(self):
        lower = {k.lower(): v for k, v in self.headers().items()}

        assert verify_slack_signature(self.BODY, lower, SECRET, 1761296000.0)

    def test_the_secret_and_the_body_both_count(self):
        assert not verify_slack_signature(self.BODY, self.headers(), SECRET + "x", 1761296000.0)
        assert not verify_slack_signature(self.BODY + b"x", self.headers(), SECRET, 1761296000.0)

    def test_an_empty_secret_refuses_a_body_signed_with_it(self):
        headers = sign(self.BODY, secret="", timestamp="1761296000")

        assert not verify_slack_signature(self.BODY, headers, "", 1761296000.0)

    def test_the_signature_must_carry_the_version_prefix(self):
        headers = self.headers()
        headers["X-Slack-Signature"] = headers["X-Slack-Signature"][3:]

        assert not verify_slack_signature(self.BODY, headers, SECRET, 1761296000.0)

    def test_a_body_that_is_not_utf8_can_be_signed_and_verified(self):
        body = b"\xff\xfe payload"
        headers = sign(body, timestamp="1761296000")

        assert verify_slack_signature(body, headers, SECRET, 1761296000.0)


# ------------------------------------------------------------------------------------------------ the whole way

class TestFromSlackToTheQuestion:
    async def test_a_press_is_queued_run_by_the_worker_and_answers_the_question(self):
        class Iam(PlatformAuthPort):
            async def authorize(self, platform, platform_user_id=None):
                if (platform, platform_user_id) != ("slack", "U0OWNER"):
                    return IAMDecision(action="reject")
                return IAMDecision(action="allow", user=UserProfile(user_id=USER, email="a@example.com", account_id=ACC))

        review = ReviewWorld()
        fact = await stale_fact(review)
        await question_on(review, fact)
        ref = ref_of(review)
        touched: List[str] = []
        adapter = HTTPModeAdapter(
            app=Tripwire("bolt_app", touched), config={"SLACK_SIGNING_SECRET": SECRET, "SLACK_BOT_TOKEN": "xoxb-test"},
            task_service=Tripwire("events_queue", touched), session_store=Tripwire("session_store", touched),
            conversation_handler=Tripwire("conversation_handler", touched), iam_service=Tripwire("iam", touched),
            worker_queue=review.queue,
        )
        app = Quart("slack_e2e")
        app.register_blueprint(adapter.get_blueprint(), url_prefix="/slack")
        payload = press(container={"message_ts": ref.message_id, "channel_id": ref.channel_id},
                        channel={"id": ref.channel_id}, message={"ts": ref.message_id})
        payload["actions"][0]["block_id"] = encode(InteractiveBinding(kind="fact_question", id="q1"))
        body = form(payload)

        resp = await app.test_client().post("/slack/interactivity", data=body, headers=sign(body))

        assert resp.status_code == 200
        [(task_type, queued)] = [(t, b) for t, b in review.enqueued if t == INTERACTIVE_CHOICE_TASK]
        worker = WorkerHandler(
            agent_worker_handler=MagicMock(), email_indexing_service=MagicMock(), notification_service=AsyncMock(),
            consolidation_service=None, coordinator=MagicMock(), agent_factory=MagicMock(), indexed_email_repo=None,
            user_repo=MagicMock(),
            interactive_choice=InteractiveChoiceService(Iam(), {"fact_question": service(review)}, review.interactive),
        )
        answer, status = await worker.handle(queued)

        assert (answer, status) == ({"status": "shown"}, 200)
        done = review.question_docs["q1"]
        assert (done["status"], done["answer"], done["answer_source"]) == ("closed", "ended", "button")
        assert review.docs[fact]["state"] == "archived"
        assert review.interactive.shown[ref].chosen_option_id == "ended"
        assert touched == []

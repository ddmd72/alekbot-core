"""s1b AB2: no chat message is lost — intake marks are released when the hand-off fails,
a busy Slack session is re-queued with a delay (never 429), and busy_requeues survives the queue."""
import asyncio
import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.adapters.gcp_task_queue import GcpTaskQueue
from src.adapters.slack import http_adapter as slack_mod
from src.adapters.slack.http_adapter import BUSY_MAX_REQUEUES, BUSY_REQUEUE_DELAY_S, HTTPModeAdapter
from src.adapters.telegram.webhook_adapter import TelegramWebhookAdapter
from src.domain.language import LanguageCode


class _FakeDedup:
    def __init__(self):
        self.marks = set()

    async def try_mark_processed(self, key):
        if key in self.marks:
            return False
        self.marks.add(key)
        return True

    async def release(self, key):
        self.marks.discard(key)


def _slack_adapter(queue):
    a = object.__new__(HTTPModeAdapter)
    a.task_service = queue
    a.dedup_store = _FakeDedup()
    a._verify_signature = lambda body, headers: True
    a._session_locks = __import__("weakref").WeakValueDictionary()
    a.app = MagicMock()
    a.slack_bot_token = "xoxb"
    a._localization = None
    return a


EVENT = {"event_id": "Ev1", "type": "event_callback",
         "event": {"type": "message", "text": "hi", "channel": "D1", "user": "U1", "ts": "1.5"}}


def _slack_request():
    req = MagicMock()
    req.get_data = AsyncMock(return_value=json.dumps(EVENT).encode())
    req.headers = {}
    return req


async def _slack_intake(adapter):
    with patch.object(slack_mod, "request", new=_slack_request()), \
            patch.object(slack_mod, "jsonify", side_effect=lambda x: x):
        return await adapter._handle_slack_event()


async def test_slack_intake_uses_a_named_task_and_never_touches_the_mark():
    queue = MagicMock()
    queue.enqueue_slack_event = AsyncMock(return_value="t")
    adapter = _slack_adapter(queue)
    adapter.dedup_store = MagicMock()
    _, status = await _slack_intake(adapter)
    assert status == 200
    assert queue.enqueue_slack_event.call_args.kwargs["task_name"] == slack_mod._task_name("Ev1")
    assert slack_mod._task_name("Ev1").startswith("slack-")
    assert adapter.dedup_store.mock_calls == []


async def test_slack_enqueue_failure_is_500_and_retry_enqueues_under_the_same_name():
    queue = MagicMock()
    queue.enqueue_slack_event = AsyncMock(side_effect=[RuntimeError("boom"), "task"])
    adapter = _slack_adapter(queue)
    _, status = await _slack_intake(adapter)
    assert status == 500
    _, status = await _slack_intake(adapter)
    assert status == 200
    names = [c.kwargs["task_name"] for c in queue.enqueue_slack_event.call_args_list]
    assert names[0] == names[1]


def test_task_names_are_deterministic_and_requeues_differ():
    assert slack_mod._task_name("E") == slack_mod._task_name("E")
    assert slack_mod._task_name("E", 2) == slack_mod._task_name("E") + "-r2"
    assert slack_mod._task_name(None) is None


def _tg_adapter(queue):
    return TelegramWebhookAdapter(token="1:abc", webhook_secret="s", dedup_store=_FakeDedup(),
                                  session_store=MagicMock(), conversation_handler=AsyncMock(),
                                  iam_service=AsyncMock(), task_queue=queue)


TG_UPDATE = {"update_id": 501, "message": {"message_id": 15, "date": 1700000000,
             "chat": {"id": 42, "type": "private"}, "from": {"id": 7, "is_bot": False, "first_name": "D"},
             "text": "hello"}}


async def _tg_intake(adapter):
    adapter._verify_webhook_signature = AsyncMock(return_value=True)
    req = MagicMock()
    req.get_json = AsyncMock(return_value=TG_UPDATE)
    with patch("src.adapters.telegram.webhook_adapter.request", new=req), \
            patch("src.adapters.telegram.webhook_adapter.jsonify", side_effect=lambda x: x):
        return await adapter._handle_telegram_update()


async def test_telegram_queued_path_uses_dedup_key_and_skips_the_mark():
    queue = AsyncMock()
    queue.enqueue_worker_task = AsyncMock(return_value=None)  # None = ALREADY_EXISTS = already queued
    adapter = _tg_adapter(queue)
    adapter.dedup_store = MagicMock()
    _, status = await _tg_intake(adapter)
    assert status == 200
    assert queue.enqueue_worker_task.call_args.kwargs["dedup_key"] == "telegram::501"
    assert adapter.dedup_store.mock_calls == []


async def test_telegram_enqueue_failure_is_500_and_retry_enqueues():
    queue = AsyncMock()
    queue.enqueue_worker_task = AsyncMock(side_effect=[RuntimeError("boom"), "task"])
    adapter = _tg_adapter(queue)
    _, status = await _tg_intake(adapter)
    assert status == 500
    _, status = await _tg_intake(adapter)
    assert status == 200


async def test_telegram_inline_path_still_uses_the_mark():
    adapter = _tg_adapter(None)
    adapter._process_message = AsyncMock()
    _, status = await _tg_intake(adapter)
    assert status == 200
    assert adapter.dedup_store.marks == {"telegram::501"}
    _, status = await _tg_intake(adapter)
    assert status == 200
    adapter._process_message.assert_awaited_once()


def _busy_adapter(queue):
    a = _slack_adapter(queue)
    a.conversation_handler = AsyncMock()
    a.iam_service = AsyncMock()
    a.iam_service.authorize.return_value = MagicMock(user=MagicMock(user_id="u1"))
    a._resolve_language = AsyncMock(return_value=(LanguageCode.EN, None, True))
    a._process_message_event = AsyncMock()
    return a


def _payload(n=None):
    p = {"event": EVENT, "session_id": "1.5"}
    if n is not None:
        p["busy_requeues"] = n
    return p


@pytest.fixture(autouse=True)
def _short_busy_wait(monkeypatch):
    monkeypatch.setattr(slack_mod, "BUSY_WAIT_S", 0.01)


async def test_busy_session_is_requeued_with_delay_and_counter():
    queue = MagicMock()
    queue.enqueue_slack_event = AsyncMock(return_value="t")
    adapter = _busy_adapter(queue)
    lock = __import__("asyncio").Lock()
    await lock.acquire()
    adapter._session_locks["1.5"] = lock
    body, status = await adapter.handle_queued_event(_payload(), {"x-trace-id": "tr_1"})
    assert status == 200
    kwargs = queue.enqueue_slack_event.call_args.kwargs
    assert kwargs["delay_seconds"] == BUSY_REQUEUE_DELAY_S == 15
    assert kwargs["busy_requeues"] == 1
    assert kwargs["session_id"] == "1.5"
    assert kwargs["event_data"] == EVENT
    assert kwargs["task_name"] == slack_mod._task_name("Ev1", 1)
    adapter._process_message_event.assert_not_awaited()


async def test_busy_counter_increments_from_payload():
    queue = MagicMock()
    queue.enqueue_slack_event = AsyncMock(return_value="t")
    adapter = _busy_adapter(queue)
    lock = __import__("asyncio").Lock()
    await lock.acquire()
    adapter._session_locks["1.5"] = lock
    _, status = await adapter.handle_queued_event(_payload(5), {})
    assert status == 200
    assert queue.enqueue_slack_event.call_args.kwargs["busy_requeues"] == 6


async def test_busy_exhausted_posts_notice_and_stops():
    queue = MagicMock()
    queue.enqueue_slack_event = AsyncMock()
    adapter = _busy_adapter(queue)
    lock = __import__("asyncio").Lock()
    await lock.acquire()
    adapter._session_locks["1.5"] = lock
    channel = MagicMock()
    channel._ui_string = MagicMock(return_value="resend please")
    channel.send_message = AsyncMock()
    with patch.object(slack_mod, "SlackResponseChannel", return_value=channel) as cls:
        _, status = await adapter.handle_queued_event(_payload(BUSY_MAX_REQUEUES), {})
    assert status == 200
    queue.enqueue_slack_event.assert_not_awaited()
    assert cls.call_args.args[1] == "D1"
    channel.send_message.assert_awaited_once()
    assert channel.send_message.call_args.args[0] == "resend please"
    adapter._process_message_event.assert_not_awaited()


async def test_busy_requeue_failure_returns_500():
    queue = MagicMock()
    queue.enqueue_slack_event = AsyncMock(side_effect=RuntimeError("boom"))
    adapter = _busy_adapter(queue)
    lock = __import__("asyncio").Lock()
    await lock.acquire()
    adapter._session_locks["1.5"] = lock
    _, status = await adapter.handle_queued_event(_payload(), {})
    assert status == 500


async def test_free_session_is_processed_not_requeued():
    queue = MagicMock()
    queue.enqueue_slack_event = AsyncMock()
    adapter = _busy_adapter(queue)
    _, status = await adapter.handle_queued_event(_payload(), {})
    assert status == 200
    adapter._process_message_event.assert_awaited_once()
    queue.enqueue_slack_event.assert_not_awaited()


async def test_busy_requeues_round_trips_through_the_cloud_tasks_payload():
    with patch("google.cloud.tasks_v2.CloudTasksClient") as cls:
        client = cls.return_value
        client.queue_path.return_value = "q"
        client.create_task.return_value = MagicMock(name="t")
        queue = GcpTaskQueue(project_id="p", location="l", queue_name="q", service_url="https://x")
        await queue.enqueue_slack_event(event_data={"event": {}}, session_id="s", delay_seconds=15, busy_requeues=3)
        await queue.enqueue_slack_event(event_data={"event": {}}, session_id="s")
        first, second = (c.kwargs["request"]["task"] for c in client.create_task.call_args_list)
    assert json.loads(first["http_request"]["body"])["busy_requeues"] == 3
    assert "schedule_time" in first
    assert json.loads(second["http_request"]["body"])["busy_requeues"] == 0


async def test_requeue_already_exists_is_200_via_the_queue_adapter():
    from google.api_core.exceptions import AlreadyExists
    with patch("google.cloud.tasks_v2.CloudTasksClient") as cls:
        client = cls.return_value
        client.queue_path.return_value = "q"
        client.task_path.side_effect = lambda *a: "/".join(a)
        client.create_task.side_effect = AlreadyExists("dup")
        queue = GcpTaskQueue(project_id="p", location="l", queue_name="q", service_url="https://x")
        name = await queue.enqueue_slack_event(event_data={"event": {}}, session_id="s", task_name="slack-abc")
    assert name == "slack-abc"
    assert client.create_task.call_args.kwargs["request"]["task"]["name"] == "p/l/q/slack-abc"


async def test_requeue_with_already_exists_returns_200():
    queue = MagicMock()
    queue.enqueue_slack_event = AsyncMock(return_value="slack-x-r1")  # adapter swallowed ALREADY_EXISTS
    adapter = _busy_adapter(queue)
    lock = asyncio.Lock()
    await lock.acquire()
    adapter._session_locks["1.5"] = lock
    _, status = await adapter.handle_queued_event(_payload(), {})
    assert status == 200


async def test_waiter_acquires_the_lock_when_released_within_the_wait(monkeypatch):
    monkeypatch.setattr(slack_mod, "BUSY_WAIT_S", 5)
    queue = MagicMock()
    queue.enqueue_slack_event = AsyncMock()
    adapter = _busy_adapter(queue)
    lock = asyncio.Lock()
    await lock.acquire()
    adapter._session_locks["1.5"] = lock
    task = asyncio.create_task(adapter.handle_queued_event(_payload(), {}))
    await asyncio.sleep(0.05)
    adapter._process_message_event.assert_not_awaited()
    lock.release()
    _, status = await task
    assert status == 200
    adapter._process_message_event.assert_awaited_once()
    queue.enqueue_slack_event.assert_not_awaited()


async def test_two_waiters_are_served_in_arrival_order(monkeypatch):
    monkeypatch.setattr(slack_mod, "BUSY_WAIT_S", 5)
    adapter = _busy_adapter(MagicMock())
    order = []

    async def record(event, session_id, **kw):
        order.append(event["text"])
    adapter._process_message_event = record
    lock = asyncio.Lock()
    await lock.acquire()
    adapter._session_locks["1.5"] = lock

    def payload(text):
        return {"event": {"event_id": text, "event": {"type": "message", "text": text, "user": "U1"}}, "session_id": "1.5"}
    t1 = asyncio.create_task(adapter.handle_queued_event(payload("first"), {}))
    await asyncio.sleep(0.02)
    t2 = asyncio.create_task(adapter.handle_queued_event(payload("second"), {}))
    await asyncio.sleep(0.02)
    lock.release()
    await asyncio.gather(t1, t2)
    assert order == ["first", "second"]


async def test_wait_timeout_falls_back_to_the_named_requeue():
    queue = MagicMock()
    queue.enqueue_slack_event = AsyncMock(return_value="t")
    adapter = _busy_adapter(queue)
    lock = asyncio.Lock()
    await lock.acquire()
    adapter._session_locks["1.5"] = lock
    _, status = await adapter.handle_queued_event(_payload(2), {})
    assert status == 200
    assert queue.enqueue_slack_event.call_args.kwargs["task_name"] == slack_mod._task_name("Ev1", 3)
    assert lock.locked()


# --- U2: events the processing path ignores never wait on the lock, re-queue, or post the notice ---

def _busy_adapter_locked():
    queue = MagicMock()
    queue.enqueue_slack_event = AsyncMock()
    adapter = _busy_adapter(queue)
    lock = asyncio.Lock()
    adapter._lock = lock
    adapter._session_locks["1.5"] = lock
    return adapter, queue, lock


def _event_payload(extra, n=None):
    ev = {"type": "message", "text": "x", "channel": "D1", "user": "U1", "ts": "1.5", **extra}
    p = {"event": {"event_id": "Ev9", "event": ev}, "session_id": "1.5"}
    if n is not None:
        p["busy_requeues"] = n
    return p


@pytest.mark.parametrize("extra", [
    {"bot_id": "B1"},
    {"subtype": "message_changed"},
    {"subtype": "message_deleted"},
    {"user": None},
])
async def test_ignored_events_return_at_once_while_the_session_is_busy(extra, monkeypatch):
    monkeypatch.setattr(slack_mod, "BUSY_WAIT_S", 5)
    adapter, queue, lock = _busy_adapter_locked()
    await lock.acquire()
    body, status = await asyncio.wait_for(adapter.handle_queued_event(_event_payload(extra), {}), 1)
    assert (body, status) == ({"ok": True}, 200)
    queue.enqueue_slack_event.assert_not_awaited()
    adapter._process_message_event.assert_not_awaited()


async def test_ignored_event_with_exhausted_requeues_posts_no_notice():
    adapter, queue, lock = _busy_adapter_locked()
    await lock.acquire()
    channel = MagicMock()
    channel.send_message = AsyncMock()
    with patch.object(slack_mod, "SlackResponseChannel", return_value=channel):
        body, status = await adapter.handle_queued_event(
            _event_payload({"subtype": "message_changed"}, BUSY_MAX_REQUEUES), {})
    assert status == 200
    assert body == {"ok": True}
    channel.send_message.assert_not_awaited()


async def test_file_share_still_waits_for_the_lock(monkeypatch):
    monkeypatch.setattr(slack_mod, "BUSY_WAIT_S", 5)
    adapter, queue, lock = _busy_adapter_locked()
    await lock.acquire()
    task = asyncio.create_task(adapter.handle_queued_event(_event_payload({"subtype": "file_share"}), {}))
    await asyncio.sleep(0.05)
    assert not task.done()
    adapter._process_message_event.assert_not_awaited()
    lock.release()
    _, status = await task
    assert status == 200
    adapter._process_message_event.assert_awaited_once()


async def test_exhausted_notice_is_posted_for_a_real_user_message():
    adapter, queue, lock = _busy_adapter_locked()
    await lock.acquire()
    channel = MagicMock()
    channel._ui_string = MagicMock(return_value="resend")
    channel.send_message = AsyncMock()
    with patch.object(slack_mod, "SlackResponseChannel", return_value=channel):
        await adapter.handle_queued_event(_event_payload({}, BUSY_MAX_REQUEUES), {})
    channel.send_message.assert_awaited_once()


async def test_unsupported_event_type_returns_at_once_while_the_session_is_busy(monkeypatch):
    monkeypatch.setattr(slack_mod, "BUSY_WAIT_S", 5)
    adapter, queue, lock = _busy_adapter_locked()
    await lock.acquire()
    body, status = await asyncio.wait_for(
        adapter.handle_queued_event(_event_payload({"type": "member_joined_channel"}), {}), 1)
    assert (body, status) == ({"ok": True}, 200)
    queue.enqueue_slack_event.assert_not_awaited()
    adapter._process_message_event.assert_not_awaited()

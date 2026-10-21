"""s1b AB2: no chat message is lost — intake marks are released when the hand-off fails,
a busy Slack session is re-queued with a delay (never 429), and busy_requeues survives the queue."""
import json
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


async def test_slack_enqueue_failure_releases_mark_and_retry_enqueues():
    queue = MagicMock()
    queue.enqueue_slack_event = AsyncMock(side_effect=[RuntimeError("boom"), "task"])
    adapter = _slack_adapter(queue)
    _, status = await _slack_intake(adapter)
    assert status == 500
    assert "Ev1" not in adapter.dedup_store.marks
    _, status = await _slack_intake(adapter)
    assert status == 200
    assert queue.enqueue_slack_event.await_count == 2
    assert "Ev1" in adapter.dedup_store.marks


async def test_slack_release_failure_does_not_mask_the_500():
    queue = MagicMock()
    queue.enqueue_slack_event = AsyncMock(side_effect=RuntimeError("boom"))
    adapter = _slack_adapter(queue)
    adapter.dedup_store.release = AsyncMock(side_effect=RuntimeError("fs down"))
    body, status = await _slack_intake(adapter)
    assert status == 500
    assert body == {"error": "boom"}


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


async def test_telegram_enqueue_failure_releases_mark_and_retry_enqueues():
    queue = AsyncMock()
    queue.enqueue_worker_task = AsyncMock(side_effect=[RuntimeError("boom"), "task"])
    adapter = _tg_adapter(queue)
    _, status = await _tg_intake(adapter)
    assert status == 500
    assert adapter.dedup_store.marks == set()
    _, status = await _tg_intake(adapter)
    assert status == 200
    assert queue.enqueue_worker_task.await_count == 2


async def test_telegram_enqueue_returning_none_counts_as_failure():
    queue = AsyncMock()
    queue.enqueue_worker_task = AsyncMock(return_value=None)
    adapter = _tg_adapter(queue)
    _, status = await _tg_intake(adapter)
    assert status == 500
    assert adapter.dedup_store.marks == set()


async def test_telegram_inline_failure_keeps_the_mark():
    adapter = _tg_adapter(None)
    adapter._process_message = AsyncMock(side_effect=RuntimeError("boom"))
    _, status = await _tg_intake(adapter)
    assert status == 500
    assert adapter.dedup_store.marks == {"telegram::501"}


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

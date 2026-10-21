"""s1b AB2 round 2: only AlreadyExists is swallowed by the chat enqueues; busy re-queue create retries
under the same name; a redelivered task hands off without waiting."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.api_core.exceptions import AlreadyExists, DeadlineExceeded, ServiceUnavailable

from src.adapters.gcp_task_queue import GcpTaskQueue
from src.adapters.slack import http_adapter as slack_mod
from src.adapters.slack.http_adapter import HTTPModeAdapter
from src.adapters.telegram.webhook_adapter import TelegramWebhookAdapter
from src.domain.language import LanguageCode

ERRORS = [DeadlineExceeded("deadline"), ServiceUnavailable("down")]


@pytest.fixture
def queue_and_client():
    with patch("google.cloud.tasks_v2.CloudTasksClient") as cls:
        client = cls.return_value
        client.queue_path.return_value = "q"
        client.task_path.return_value = "q/tasks/t"
        yield GcpTaskQueue(project_id="p", location="l", queue_name="q", service_url="https://x"), client


@pytest.mark.parametrize("error", ERRORS, ids=lambda e: type(e).__name__)
async def test_enqueue_slack_event_raises_on_transport_errors(queue_and_client, error):
    queue, client = queue_and_client
    client.create_task.side_effect = error
    with pytest.raises(type(error)):
        await queue.enqueue_slack_event({"event": {}}, "s", task_name="slack-x")


@pytest.mark.parametrize("error", ERRORS, ids=lambda e: type(e).__name__)
async def test_enqueue_worker_task_raises_on_transport_errors(queue_and_client, error):
    queue, client = queue_and_client
    client.create_task.side_effect = error
    with pytest.raises(type(error)):
        await queue.enqueue_worker_task("telegram_update", {"update": {}}, dedup_key="telegram::1")


async def test_already_exists_is_the_only_swallowed_error(queue_and_client):
    queue, client = queue_and_client
    client.create_task.side_effect = AlreadyExists("dup")
    assert await queue.enqueue_slack_event({"event": {}}, "s", task_name="slack-x") == "slack-x"
    assert await queue.enqueue_worker_task("telegram_update", {}, dedup_key="k") is None


EVENT = {"event_id": "Ev1", "type": "event_callback",
         "event": {"type": "message", "text": "hi", "channel": "D1", "user": "U1", "ts": "1.5"}}


@pytest.mark.parametrize("error", ERRORS, ids=lambda e: type(e).__name__)
async def test_slack_intake_answers_500_on_transport_errors(queue_and_client, error):
    queue, client = queue_and_client
    client.create_task.side_effect = error
    adapter = object.__new__(HTTPModeAdapter)
    adapter.task_service = queue
    adapter._verify_signature = lambda body, headers: True
    req = MagicMock()
    req.get_data = AsyncMock(return_value=json.dumps(EVENT).encode())
    req.headers = {}
    with patch.object(slack_mod, "request", new=req), patch.object(slack_mod, "jsonify", side_effect=lambda x: x):
        _, status = await adapter._handle_slack_event()
    assert status == 500


@pytest.mark.parametrize("error", ERRORS, ids=lambda e: type(e).__name__)
async def test_telegram_intake_answers_500_on_transport_errors(queue_and_client, error):
    queue, client = queue_and_client
    client.create_task.side_effect = error
    adapter = TelegramWebhookAdapter(token="1:abc", webhook_secret="s", dedup_store=MagicMock(),
                                     session_store=MagicMock(), conversation_handler=AsyncMock(),
                                     iam_service=AsyncMock(), task_queue=queue)
    adapter._verify_webhook_signature = AsyncMock(return_value=True)
    update = {"update_id": 501, "message": {"message_id": 15, "date": 1700000000,
              "chat": {"id": 42, "type": "private"}, "from": {"id": 7, "is_bot": False, "first_name": "D"},
              "text": "hello"}}
    req = MagicMock()
    req.get_json = AsyncMock(return_value=update)
    with patch("src.adapters.telegram.webhook_adapter.request", new=req), \
            patch("src.adapters.telegram.webhook_adapter.jsonify", side_effect=lambda x: x):
        _, status = await adapter._handle_telegram_update()
    assert status == 500


def _busy_adapter(queue):
    a = object.__new__(HTTPModeAdapter)
    a.task_service = queue
    a._session_locks = __import__("weakref").WeakValueDictionary()
    a.app = MagicMock()
    a.slack_bot_token = "x"
    a._localization = None
    a._resolve_language = AsyncMock(return_value=(LanguageCode.EN, None, True))
    a._process_message_event = AsyncMock()
    return a


async def _held(adapter):
    lock = asyncio.Lock()
    await lock.acquire()
    adapter._session_locks["1.5"] = lock
    return lock


@pytest.fixture
def fast(monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
    monkeypatch.setattr(slack_mod.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(slack_mod, "BUSY_WAIT_S", 0.01)
    return sleeps


PAYLOAD = {"event": EVENT, "session_id": "1.5"}


async def test_requeue_create_is_retried_under_the_same_name_then_succeeds(fast):
    queue = MagicMock()
    queue.enqueue_slack_event = AsyncMock(side_effect=[DeadlineExceeded("x"), ServiceUnavailable("y"), "t"])
    adapter = _busy_adapter(queue)
    lock = await _held(adapter)  # noqa: F841 - strong ref keeps the weak-dict entry
    _, status = await adapter.handle_queued_event(PAYLOAD, {})
    assert status == 200
    names = [c.kwargs["task_name"] for c in queue.enqueue_slack_event.call_args_list]
    assert names == [slack_mod._task_name("Ev1", 1)] * 3
    assert fast == [0.5, 1.0]


async def test_requeue_all_attempts_failing_is_500(fast):
    queue = MagicMock()
    queue.enqueue_slack_event = AsyncMock(side_effect=DeadlineExceeded("x"))
    adapter = _busy_adapter(queue)
    lock = await _held(adapter)  # noqa: F841 - strong ref keeps the weak-dict entry
    _, status = await adapter.handle_queued_event(PAYLOAD, {})
    assert status == 500
    assert queue.enqueue_slack_event.await_count == 4
    assert fast == [0.5, 1.0, 2.0]


async def test_redelivery_finding_the_session_busy_hands_off_without_waiting(monkeypatch):
    monkeypatch.setattr(slack_mod, "BUSY_WAIT_S", 60)
    queue = MagicMock()
    queue.enqueue_slack_event = AsyncMock(return_value="t")
    adapter = _busy_adapter(queue)
    lock = await _held(adapter)  # noqa: F841 - strong ref keeps the weak-dict entry
    _, status = await asyncio.wait_for(
        adapter.handle_queued_event(PAYLOAD, {"X-CloudTasks-TaskRetryCount": "1"}), 2)
    assert status == 200
    assert queue.enqueue_slack_event.call_args.kwargs["task_name"] == slack_mod._task_name("Ev1", 1)


async def test_redelivery_with_a_free_session_processes_normally():
    queue = MagicMock()
    queue.enqueue_slack_event = AsyncMock()
    adapter = _busy_adapter(queue)
    _, status = await adapter.handle_queued_event(PAYLOAD, {"x-cloudtasks-taskretrycount": "2"})
    assert status == 200
    adapter._process_message_event.assert_awaited_once()
    queue.enqueue_slack_event.assert_not_awaited()

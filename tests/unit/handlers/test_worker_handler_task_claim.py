"""WorkerHandler claims every Cloud Tasks delivery by task name and is the only dispatcher.

Decision record: docs/04_solution_strategy/decisions/worker_task_dedup.md.
The claim is a TaskClaimStore lease (S1b task L): a kept claim is now one marked `done`.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.slack.http_adapter import HTTPModeAdapter
from src.domain.agent import AgentResponse
from src.domain.cloud_task_delivery import CloudTaskDelivery
from src.handlers.agent_worker_handler import AgentWorkerHandler
from src.handlers.worker_handler import WorkerHandler
from src.ports.task_claim_store import TaskClaimStore


class _MemoryClaims(TaskClaimStore):
    """In-memory TaskClaimStore with the adapter's semantics, one process (no expiry)."""

    def __init__(self):
        self.claims = {}  # key -> {"attempt": str, "state": "running" | "done"}

    async def try_claim(self, key, attempt):
        held = self.claims.get(key)
        if held is not None:
            if held["attempt"] == attempt:
                return True
            if held["state"] == "done":
                return False
        # New, or a running claim: the caller guarantees no live attempt of this key here, so it is dead.
        self.claims[key] = {"attempt": attempt, "state": "running"}
        return True

    async def mark_done(self, key, attempt):
        held = self.claims.get(key)
        if held is not None and held["attempt"] == attempt:
            held["state"] = "done"

    async def release(self, key, attempt):
        held = self.claims.get(key)
        if held is not None and held["attempt"] == attempt:
            del self.claims[key]

    def done_keys(self):
        return {k for k, v in self.claims.items() if v["state"] == "done"}


def _claimed_attempt(store, task_name):
    """The attempt id the handler claimed `task_name` with (asserting the claim call's shape)."""
    store.try_claim.assert_awaited_once()
    key, attempt = store.try_claim.call_args.args
    assert key == task_name
    assert isinstance(attempt, str) and attempt
    return attempt


def _delivery(name="task-1"):
    return CloudTaskDelivery(task_name=name, retry_count=0, execution_count=0)


def _handler(claims=None, agent_worker=None):
    return WorkerHandler(
        agent_worker_handler=agent_worker or MagicMock(),
        email_indexing_service=MagicMock(),
        notification_service=AsyncMock(),
        consolidation_service=None,
        coordinator=MagicMock(),
        agent_factory=AsyncMock(),
        indexed_email_repo=None,
        user_repo=MagicMock(),
        task_claims=claims,
    )


def _agent_worker(result=None):
    aw = MagicMock()
    aw.handle_task = AsyncMock(return_value=result or {"status": "success"})
    return aw


_AGENT_PAYLOAD = {"task_type": "agent_execution", "context": {"user_id": "u1"}}
_SLACK_PAYLOAD = {"event": {"event": {"type": "message"}}, "session_id": "u1:C1"}


async def _call_counts(handler, payload, runner):
    """Deliver twice with one task name; return how many times the body ran."""
    first = await handler.handle(payload, delivery=_delivery(), headers={})
    second = await handler.handle(payload, delivery=_delivery(), headers={})
    return first, second, runner.await_count


async def test_agent_execution_second_delivery_is_duplicate():
    aw = _agent_worker()
    h = _handler(_MemoryClaims(), aw)
    first, second, runs = await _call_counts(h, _AGENT_PAYLOAD, aw.handle_task)
    assert first == ({"status": "success"}, 200)
    assert second == ({"status": "duplicate"}, 200)
    assert runs == 1


async def test_daily_email_review_second_delivery_is_duplicate():
    h = _handler(_MemoryClaims())
    h._handle_daily_email_review = AsyncMock(return_value=({"status": "ok"}, 200))
    payload = {"task_type": "daily_email_review", "user_id": "u1"}
    _, second, runs = await _call_counts(h, payload, h._handle_daily_email_review)
    assert second == ({"status": "duplicate"}, 200)
    assert runs == 1


async def test_execute_reminder_second_delivery_is_duplicate():
    h = _handler(_MemoryClaims())
    h._handle_execute_reminder = AsyncMock(return_value=({"status": "ok"}, 200))
    payload = {"task_type": "execute_reminder", "note_id": "n1"}
    _, second, runs = await _call_counts(h, payload, h._handle_execute_reminder)
    assert second == ({"status": "duplicate"}, 200)
    assert runs == 1


async def test_telegram_update_second_delivery_is_duplicate():
    h = _handler(_MemoryClaims())
    tg = AsyncMock(return_value=({"ok": True}, 200))
    h.attach_telegram_worker(tg)
    _, second, runs = await _call_counts(h, {"task_type": "telegram_update"}, tg)
    assert second == ({"status": "duplicate"}, 200)
    assert runs == 1


async def test_queued_slack_event_second_delivery_is_duplicate_and_gets_headers():
    h = _handler(_MemoryClaims())
    sl = AsyncMock(return_value=({"ok": True}, 200))
    h.attach_slack_worker(sl)
    first = await h.handle(_SLACK_PAYLOAD, delivery=_delivery(), headers={"x-trace-id": "t"})
    second = await h.handle(_SLACK_PAYLOAD, delivery=_delivery(), headers={"x-trace-id": "t"})
    assert first == ({"ok": True}, 200)
    assert second == ({"status": "duplicate"}, 200)
    sl.assert_awaited_once_with(_SLACK_PAYLOAD, {"x-trace-id": "t"})


async def test_raise_releases_claim_and_propagates_so_next_delivery_runs():
    aw = MagicMock()
    aw.handle_task = AsyncMock(side_effect=[RuntimeError("boom"), {"status": "success"}])
    h = _handler(_MemoryClaims(), aw)
    with pytest.raises(RuntimeError):
        await h.handle(_AGENT_PAYLOAD, delivery=_delivery())
    body, status = await h.handle(_AGENT_PAYLOAD, delivery=_delivery())
    assert (body, status) == ({"status": "success"}, 200)
    assert aw.handle_task.await_count == 2


@pytest.mark.parametrize("failing_status", [500, 503, 429])
async def test_retryable_status_releases_claim(failing_status):
    store = _MemoryClaims()
    h = _handler(store)
    sl = AsyncMock(side_effect=[({"e": 1}, failing_status), ({"ok": True}, 200)])
    h.attach_slack_worker(sl)
    assert (await h.handle(_SLACK_PAYLOAD, delivery=_delivery()))[1] == failing_status
    assert store.claims == {}
    assert (await h.handle(_SLACK_PAYLOAD, delivery=_delivery())) == ({"ok": True}, 200)
    assert sl.await_count == 2


@pytest.mark.parametrize("kept_status", [200, 400])
async def test_claim_marked_done_on_success_and_other_4xx(kept_status):
    store = _MemoryClaims()
    h = _handler(store)
    h.attach_slack_worker(AsyncMock(return_value=({"x": 1}, kept_status)))
    await h.handle(_SLACK_PAYLOAD, delivery=_delivery())
    assert store.done_keys() == {"task-1"}


async def test_no_delivery_means_no_claim_and_runs_every_time():
    store = _MemoryClaims()
    aw = _agent_worker()
    h = _handler(store, aw)
    await h.handle(_AGENT_PAYLOAD)
    await h.handle(_AGENT_PAYLOAD)
    assert aw.handle_task.await_count == 2
    assert store.claims == {}


async def test_claim_store_error_fails_open():
    store = AsyncMock(spec=TaskClaimStore)
    store.try_claim = AsyncMock(side_effect=RuntimeError("firestore down"))
    aw = _agent_worker()
    h = _handler(store, aw)
    assert await h.handle(_AGENT_PAYLOAD, delivery=_delivery()) == ({"status": "success"}, 200)
    aw.handle_task.assert_awaited_once()


async def test_release_failure_does_not_mask_the_original_exception():
    store = AsyncMock(spec=TaskClaimStore)
    store.try_claim = AsyncMock(return_value=True)
    store.release = AsyncMock(side_effect=RuntimeError("release failed"))
    aw = MagicMock()
    aw.handle_task = AsyncMock(side_effect=ValueError("original"))
    h = _handler(store, aw)
    with pytest.raises(ValueError, match="original"):
        await h.handle(_AGENT_PAYLOAD, delivery=_delivery())


async def test_unknown_task_type_is_400_and_never_reaches_slack():
    h = _handler()
    sl = AsyncMock()
    h.attach_slack_worker(sl)
    assert await h.handle({"task_type": "nope", **_SLACK_PAYLOAD}) == (
        {"error": "unknown task_type"}, 400)
    assert await h.handle({}) == ({"error": "unknown task_type"}, 400)
    sl.assert_not_awaited()


async def test_slack_shaped_payload_without_task_type_goes_to_slack():
    h = _handler()
    sl = AsyncMock(return_value=({"ok": True}, 200))
    h.attach_slack_worker(sl)
    assert await h.handle(_SLACK_PAYLOAD, headers={"h": "1"}) == ({"ok": True}, 200)
    sl.assert_awaited_once_with(_SLACK_PAYLOAD, {"h": "1"})


async def test_unattached_workers_answer_503():
    h = _handler()
    assert (await h.handle({"task_type": "telegram_update"}))[1] == 503
    assert (await h.handle(_SLACK_PAYLOAD))[1] == 503


# --- HTTPModeAdapter.handle_queued_event (the Slack body, no Quart request) ---

@pytest.fixture
def slack_adapter():
    return HTTPModeAdapter(
        app=AsyncMock(),
        config={"SLACK_SIGNING_SECRET": "s", "SLACK_BOT_TOKEN": "xoxb-test"},
        task_service=AsyncMock(),
        session_store=AsyncMock(),
        conversation_handler=AsyncMock(),
        iam_service=AsyncMock(),
    )


async def test_queued_event_empty_payload_is_400(slack_adapter):
    assert await slack_adapter.handle_queued_event({}, {}) == (
        {"ok": False, "error": "Empty payload"}, 400)


async def test_queued_event_missing_fields_is_400(slack_adapter):
    assert await slack_adapter.handle_queued_event({"session_id": "s"}, {}) == (
        {"ok": False, "error": "Missing required fields"}, 400)


async def test_queued_event_busy_session_waits_then_requeues_and_keeps_the_claim(
        slack_adapter, monkeypatch):
    # S1b AB2: a busy session is no longer 429 (the queue's quick retries burn out inside one turn).
    # The delivery waits in-process for the lock, then hands the event to a named, delayed task and
    # answers 200, so WorkerHandler keeps its claim and a redelivery of this task is a duplicate.
    from src.adapters.slack import http_adapter as slack_mod
    monkeypatch.setattr(slack_mod, "BUSY_WAIT_S", 0.01)
    slack_adapter._process_message_event = AsyncMock()
    slack_adapter.task_service.enqueue_slack_event = AsyncMock(return_value="t")
    lock = asyncio.Lock()
    await lock.acquire()
    slack_adapter._session_locks["u1:C1"] = lock
    store = _MemoryClaims()
    h = _handler(store)
    h.attach_slack_worker(slack_adapter.handle_queued_event)
    payload = {"event": {"event_id": "E1", "event": {"type": "message", "text": "hi", "user": "U1"}},
               "session_id": "u1:C1"}

    assert await h.handle(payload, delivery=_delivery(), headers={}) == (
        {"ok": True, "requeued": 1}, 200)

    slack_adapter.task_service.enqueue_slack_event.assert_awaited_once()
    kwargs = slack_adapter.task_service.enqueue_slack_event.call_args.kwargs
    assert kwargs["event_data"] == payload["event"]
    assert kwargs["session_id"] == "u1:C1"
    assert kwargs["busy_requeues"] == 1
    assert kwargs["delay_seconds"] == slack_mod.BUSY_REQUEUE_DELAY_S
    assert kwargs["task_name"] == slack_mod._task_name("E1", 1)
    slack_adapter._process_message_event.assert_not_awaited()
    assert store.done_keys() == {"task-1"}
    assert await h.handle(payload, delivery=_delivery(), headers={}) == ({"status": "duplicate"}, 200)
    slack_adapter.task_service.enqueue_slack_event.assert_awaited_once()


async def test_queued_event_success_is_200_and_processes_message(slack_adapter):
    slack_adapter._process_message_event = AsyncMock()
    payload = {"event": {"event_id": "E1", "event": {"type": "message", "text": "hi", "user": "U1"}},
               "session_id": "u1:C1"}
    assert await slack_adapter.handle_queued_event(payload, {}) == ({"ok": True}, 200)
    slack_adapter._process_message_event.assert_awaited_once()


async def test_queued_event_bot_message_is_200_without_processing(slack_adapter):
    slack_adapter._process_message_event = AsyncMock()
    payload = {"event": {"event": {"type": "message", "bot_id": "B1"}}, "session_id": "u1:C1"}
    assert await slack_adapter.handle_queued_event(payload, {}) == ({"ok": True}, 200)
    slack_adapter._process_message_event.assert_not_awaited()


async def test_queued_event_unexpected_error_is_500(slack_adapter):
    slack_adapter._process_message_event = AsyncMock(side_effect=RuntimeError("bad"))
    payload = {"event": {"event": {"type": "message", "user": "U1"}}, "session_id": "u1:C1"}
    assert await slack_adapter.handle_queued_event(payload, {}) == ({"error": "bad"}, 500)


# --- agent_execution through the real AgentWorkerHandler ---
# Moved from test_agent_worker_handler_task_dedup.py when the claim moved here (S1b task B).
# Incident 2026-10-09: one create_html_page task (single enqueue) was delivered twice, 208 s
# apart, while the first attempt was still running → two reports.

_HTML_PAYLOAD = {
    "task_type": "agent_execution",
    "agent_id": "html_page_generator_agent",
    "intent": "generic",
    "query": "q",
    "context": {"user_id": "u1", "account_id": "a1"},
}
_HTML_DELIVERY = CloudTaskDelivery(task_name="0038899772573220363", retry_count=0, execution_count=0)


def _store(claimed=True):
    store = AsyncMock(spec=TaskClaimStore)
    store.try_claim = AsyncMock(return_value=claimed)
    return store


def _real_agent_worker(route_result=None, route_error=None):
    coordinator = MagicMock()
    if route_error is not None:
        coordinator.route_message = AsyncMock(side_effect=route_error)
    else:
        coordinator.route_message = AsyncMock(return_value=route_result or AgentResponse.success(
            task_id="t", agent_id="a", result=None))
    return AgentWorkerHandler(coordinator=coordinator), coordinator


async def test_agent_execution_first_delivery_claims_by_task_name_and_runs_the_agent():
    store = _store(claimed=True)
    aw, coordinator = _real_agent_worker()
    body, status = await _handler(store, aw).handle(_HTML_PAYLOAD, delivery=_HTML_DELIVERY)

    attempt = _claimed_attempt(store, "0038899772573220363")
    coordinator.route_message.assert_awaited_once()
    assert body["status"] == "success"
    assert status == 200
    store.mark_done.assert_awaited_once_with("0038899772573220363", attempt)


async def test_agent_execution_redelivery_is_skipped_and_keeps_the_claim():
    store = _store(claimed=False)
    aw, coordinator = _real_agent_worker()
    result = await _handler(store, aw).handle(_HTML_PAYLOAD, delivery=_HTML_DELIVERY)

    coordinator.route_message.assert_not_awaited()
    assert result == ({"status": "duplicate"}, 200)
    store.release.assert_not_awaited()
    store.mark_done.assert_not_awaited()  # the claim belongs to the live/finished attempt


async def test_agent_execution_raising_attempt_releases_the_claim_by_task_name():
    """An exception becomes a 500 and Cloud Tasks retries — that retry must not be skipped."""
    store = _store(claimed=True)
    aw, _ = _real_agent_worker(route_error=RuntimeError("boom"))

    with pytest.raises(RuntimeError):
        await _handler(store, aw).handle(_HTML_PAYLOAD, delivery=_HTML_DELIVERY)

    attempt = _claimed_attempt(store, "0038899772573220363")
    store.release.assert_awaited_once_with("0038899772573220363", attempt)
    store.mark_done.assert_not_awaited()


async def test_agent_execution_failed_agent_response_keeps_the_claim():
    """A FAILED response returns 200 and the user is notified — Cloud Tasks does not retry,
    so a later delivery is a duplicate and must stay skipped."""
    store = _store(claimed=True)
    aw, _ = _real_agent_worker(route_result=AgentResponse.failure(
        task_id="t", agent_id="a", error="provider error"))

    body, status = await _handler(store, aw).handle(_HTML_PAYLOAD, delivery=_HTML_DELIVERY)

    assert body["status"] == "failed"
    assert status == 200
    store.release.assert_not_awaited()
    attempt = _claimed_attempt(store, "0038899772573220363")
    store.mark_done.assert_awaited_once_with("0038899772573220363", attempt)


async def test_agent_execution_release_error_still_reraises_the_agent_error():
    store = _store(claimed=True)
    store.release = AsyncMock(side_effect=RuntimeError("firestore down"))
    aw, _ = _real_agent_worker(route_error=ValueError("agent boom"))

    with pytest.raises(ValueError, match="agent boom"):
        await _handler(store, aw).handle(_HTML_PAYLOAD, delivery=_HTML_DELIVERY)


async def test_delivery_without_a_store_runs_without_claim():
    aw, coordinator = _real_agent_worker()
    body, status = await _handler(None, aw).handle(_HTML_PAYLOAD, delivery=_HTML_DELIVERY)

    coordinator.route_message.assert_awaited_once()
    assert (body["status"], status) == ("success", 200)


async def test_no_delivery_with_a_store_never_touches_the_store():
    store = _store()
    aw, coordinator = _real_agent_worker()
    await _handler(store, aw).handle(_HTML_PAYLOAD, delivery=None)

    coordinator.route_message.assert_awaited_once()
    store.try_claim.assert_not_awaited()
    store.mark_done.assert_not_awaited()
    store.release.assert_not_awaited()


async def test_release_failure_on_a_retryable_status_returns_the_original_result():
    store = _store(claimed=True)
    store.release = AsyncMock(side_effect=RuntimeError("firestore down"))
    h = _handler(store)
    h.attach_slack_worker(AsyncMock(return_value=({"e": 1}, 500)))

    assert await h.handle(_SLACK_PAYLOAD, delivery=_delivery()) == ({"e": 1}, 500)
    store.release.assert_awaited_once_with("task-1", _claimed_attempt(store, "task-1"))

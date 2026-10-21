"""A failed deep-research Cloud Run Job tells the user in the origin channel (S1b task E3)."""
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from quart import Quart

import job_main
from src.domain.agent import AgentResponse, AgentStatus
from src.domain.notification_kind import NotificationKind
from src.handlers.worker_handler import WorkerHandler
from src.services.deep_research_delivery import deliver_deep_research
from src.web.deep_research_webhooks import create_deep_research_webhooks_blueprint


# --- WorkerHandler ---------------------------------------------------------

def _worker(delivered=True):
    notification = AsyncMock()
    notification.notify.return_value = MagicMock(delivered=delivered, error=None if delivered else "boom")
    worker = WorkerHandler(
        agent_worker_handler=AsyncMock(), email_indexing_service=MagicMock(),
        notification_service=notification, consolidation_service=None,
        coordinator=MagicMock(), agent_factory=AsyncMock(),
        indexed_email_repo=None, user_repo=MagicMock(),
    )
    return worker, notification


_PAYLOAD = {"task_type": "deep_research_failed", "user_id": "u1", "account_id": "a1",
            "session_id": "u1:C9", "origin_platform": "telegram", "query": "tides", "reason": "agent_failed"}


class TestWorkerHandlerDeepResearchFailed:
    async def test_valid_payload_notifies_origin_channel_once(self):
        worker, notification = _worker()
        body, status = await worker._dispatch(dict(_PAYLOAD), {})
        assert status == 200
        notification.notify.assert_awaited_once()
        kw = notification.notify.await_args.kwargs
        assert kw["kind"] == NotificationKind.DEEP_RESEARCH
        assert kw["channel_id_override"] == "C9"
        assert kw["platform_override"] == "telegram"
        assert "tides" in kw["system_alert"] and "again" in kw["system_alert"]
        assert "agent_failed" not in kw["system_alert"]

    @pytest.mark.parametrize("drop", ["user_id", "account_id"])
    async def test_missing_ids_is_400(self, drop):
        worker, notification = _worker()
        payload = {k: v for k, v in _PAYLOAD.items() if k != drop}
        _, status = await worker._dispatch(payload, {})
        assert status == 400
        notification.notify.assert_not_awaited()

    async def test_not_delivered_is_500(self):
        worker, _ = _worker(delivered=False)
        _, status = await worker._dispatch(dict(_PAYLOAD), {})
        assert status == 500

    async def test_old_payload_without_platform_has_no_platform_override(self):
        worker, notification = _worker()
        payload = {k: v for k, v in _PAYLOAD.items() if k != "origin_platform"}
        _, status = await worker._dispatch(payload, {})
        assert status == 200
        assert notification.notify.await_args.kwargs["platform_override"] is None
        assert notification.notify.await_args.kwargs["channel_id_override"] == "C9"


# --- job_main --------------------------------------------------------------

_CTX = {"user_id": "u1", "account_id": "a1", "session_id": "u1:C9",
        "origin_platform": "slack", "job_id": "job-7", "original_query": "tides"}


async def _run(monkeypatch, response=None, deliver_exc=None, enqueue_exc=None):
    monkeypatch.setenv("JOB_QUERY", "q")
    monkeypatch.setenv("JOB_CONTEXT_JSON", json.dumps(_CTX))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("GCS_MEDIA_BUCKET", raising=False)
    agent = MagicMock()
    agent.agent_id = "x"
    agent.execute = AsyncMock(return_value=response)
    queue = MagicMock()
    queue.enqueue_worker_task = AsyncMock(side_effect=enqueue_exc)
    with patch.object(job_main, "ClaudeDeepResearchRunnerAgent", return_value=agent), \
         patch.object(job_main, "_build_task_queue", return_value=queue), \
         patch.object(job_main.anthropic, "AsyncAnthropic"), \
         patch.object(job_main, "_capture_research_result", AsyncMock()), \
         patch.object(job_main, "_record_billing", AsyncMock()), \
         patch.object(job_main, "deliver_deep_research", AsyncMock(side_effect=deliver_exc)):
        try:
            await job_main._run_job()
            code = None
        except SystemExit as e:
            code = e.code
    return queue, code


def _resp(status=AgentStatus.SUCCESS, result=None, error=None):
    return AgentResponse(task_id="t", agent_id="x", status=status, confidence=1.0,
                         result=result, error=error)


class TestJobMainFailureNotice:
    async def _assert_one_notice(self, queue, code, reason):
        assert code == 1
        queue.enqueue_worker_task.assert_awaited_once()
        args, kwargs = queue.enqueue_worker_task.await_args
        assert args[0] == "deep_research_failed"
        assert args[1] == {"user_id": "u1", "account_id": "a1", "session_id": "u1:C9",
                           "origin_platform": "slack", "query": "tides", "reason": reason}
        assert kwargs["dedup_key"] == "deep_research_failed:job-7"

    async def test_agent_failure(self, monkeypatch):
        queue, code = await _run(monkeypatch, _resp(AgentStatus.FAILED, error="x"))
        await self._assert_one_notice(queue, code, "agent_failed")

    async def test_empty_result(self, monkeypatch):
        queue, code = await _run(monkeypatch, _resp(result={"text": ""}))
        await self._assert_one_notice(queue, code, "empty_result")

    async def test_delivery_failure(self, monkeypatch):
        queue, code = await _run(monkeypatch, _resp(result={"text": "r"}), deliver_exc=RuntimeError("x"))
        await self._assert_one_notice(queue, code, "delivery_failed")

    async def test_enqueue_failure_still_exits_1_and_logs_error(self, monkeypatch, caplog):
        with caplog.at_level(logging.ERROR):
            queue, code = await _run(monkeypatch, _resp(AgentStatus.FAILED, error="x"),
                                     enqueue_exc=RuntimeError("down"))
        assert code == 1
        assert any("Failure notice not enqueued" in r.getMessage() and r.levelno == logging.ERROR
                   for r in caplog.records)

    async def test_success_enqueues_nothing(self, monkeypatch):
        queue, code = await _run(monkeypatch, _resp(result={"text": "r"}))
        assert code is None
        queue.enqueue_worker_task.assert_not_awaited()


# --- deliver_deep_research / webhook (carried over from task E review) -----

class TestDeliveryFailureNotifier:
    async def _deliver(self, **kw):
        queue = AsyncMock()
        queue.enqueue_agent_task.side_effect = RuntimeError("down")
        queue.enqueue_agent_task_with_context = queue.enqueue_agent_task
        queue.enqueue_worker_task.side_effect = RuntimeError("down")
        await deliver_deep_research(result_text="r", user_id="u1", account_id="a1", query="q",
                                    task_queue=queue, session_id="u1:C9", **kw)

    async def test_falls_back_to_notification_when_no_failure_notification(self):
        notification = AsyncMock()
        await self._deliver(notification=notification)
        notification.notify.assert_awaited_once()
        alert = notification.notify.await_args.kwargs["system_alert"]
        assert "retried" not in alert and "again" in alert

    async def test_failure_notification_takes_precedence(self):
        notification, failure = AsyncMock(), AsyncMock()
        await self._deliver(notification=notification, failure_notification=failure)
        failure.notify.assert_awaited_once()
        notification.notify.assert_not_awaited()

    async def test_openai_webhook_passes_failure_notification(self):
        service = AsyncMock()
        app = Quart("t")
        app.register_blueprint(create_deep_research_webhooks_blueprint(
            notification_service=service, webhook_secret=None,
            media_storage=MagicMock(), task_queue=AsyncMock()))
        payload = {"type": "response.completed", "data": {"id": "r1", "output_text": "t", "metadata": {
            "user_id": "u", "account_id": "a", "query": "q", "session_id": "u:C1"}}}
        with patch("src.web.deep_research_webhooks.deliver_deep_research", new_callable=AsyncMock) as d:
            async with app.test_client() as c:
                await c.post("/webhooks/openai/deep-research", data=json.dumps(payload),
                             headers={"Content-Type": "application/json"})
        assert d.call_args.kwargs["failure_notification"] is service

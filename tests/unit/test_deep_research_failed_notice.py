"""A failed deep-research Cloud Run Job tells the user in the origin channel (S1b task E3)."""
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import job_main
from src.domain.exceptions import OAuthGrantRevokedError
from src.domain.agent import AgentResponse, AgentStatus
from src.domain.notification_kind import NotificationKind
from src.handlers.worker_handler import WorkerHandler
from src.services.deep_research_delivery import deliver_deep_research
from src.composition.connector_registry import ConnectorRegistry


# --- WorkerHandler ---------------------------------------------------------

def _worker(delivered=True):
    notification = AsyncMock()
    notification.notify.return_value = MagicMock(delivered=delivered, sent=delivered,
                                                     error=None if delivered else "boom")
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


# --- deliver_deep_research / webhook worker task (carried over from task E review)

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
        """The OpenAI delivery now runs in the webhook's worker task (S1b W): its notifier is passed on."""
        service = AsyncMock()
        worker = WorkerHandler(
            agent_worker_handler=AsyncMock(), email_indexing_service=MagicMock(),
            notification_service=service, consolidation_service=None,
            coordinator=MagicMock(), agent_factory=AsyncMock(),
            indexed_email_repo=None, user_repo=MagicMock(), task_dispatch=AsyncMock(),
            media_storage=MagicMock(), deep_research_fetch=AsyncMock(return_value={"output_text": "t"}),
        )
        payload = {"task_type": "deep_research_webhook_event", "event_type": "response.completed",
                   "job_id": "r1", "user_id": "u", "account_id": "a", "query": "q", "session_id": "u:C1",
                   "origin_platform": ""}
        with patch("src.handlers.worker_handler.deliver_deep_research", new_callable=AsyncMock) as d:
            await worker._dispatch(payload, {})
        assert d.call_args.kwargs["failure_notification"] is service


# --- ensure_agents_for_user runs before notify (cold instance) -------------

def _ordered_worker(**kw):
    log = []
    notification = AsyncMock()

    async def _notify(**_k):
        log.append("notify")
        return MagicMock(delivered=True, error=None)

    notification.notify.side_effect = _notify
    factory = AsyncMock()

    async def _ensure(_uid):
        log.append("ensure")

    factory.ensure_agents_for_user.side_effect = _ensure
    worker = WorkerHandler(
        agent_worker_handler=AsyncMock(), email_indexing_service=MagicMock(),
        notification_service=notification, consolidation_service=None,
        coordinator=MagicMock(), agent_factory=factory,
        indexed_email_repo=None, user_repo=MagicMock(), **kw,
    )
    return worker, log


class TestEnsureBeforeNotify:
    async def test_deep_research_failed(self):
        worker, log = _ordered_worker()
        _, status = await worker.handle(dict(_PAYLOAD))
        assert status == 200
        assert log[:2] == ["ensure", "notify"]

    async def test_video_generation_failed_branch(self):
        port = AsyncMock()
        port.get_status.return_value = MagicMock(status="failed", error="bad", data=None, duration_s=None)
        registry = MagicMock()
        registry.get.return_value = port
        worker, log = _ordered_worker(video_registry=registry, task_dispatch=AsyncMock())
        _, status = await worker.handle({
            "task_type": "video_generation_polling", "request_id": "r1", "user_id": "u1",
            "account_id": "a1", "session_id": "u1:C9", "attempt": 0})
        assert status == 200
        assert log[:2] == ["ensure", "notify"]

    async def test_gmail_reconnect_flag_notice(self):
        creds = MagicMock(needs_reconnect=False)
        oauth = AsyncMock()
        oauth.list_for_user.return_value = [creds]
        email_review = AsyncMock()
        email_review.fetch_review_payload.side_effect = OAuthGrantRevokedError("revoked")
        worker, log = _ordered_worker(oauth_credentials=oauth, email_review=email_review, mail_connector=ConnectorRegistry().get("gmail"))
        await worker.handle({"task_type": "daily_email_review", "user_id": "u1", "account_id": "a1"})
        assert log[:2] == ["ensure", "notify"]


# --- job_main with the real deliver_deep_research --------------------------

class TestJobMainRealDelivery:
    async def _run_real(self, monkeypatch, enqueue_exc):
        monkeypatch.setenv("JOB_QUERY", "q")
        monkeypatch.setenv("JOB_CONTEXT_JSON", json.dumps(_CTX))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        monkeypatch.delenv("GCS_MEDIA_BUCKET", raising=False)
        agent = MagicMock()
        agent.agent_id = "x"
        agent.execute = AsyncMock(return_value=_resp(result={"text": "report"}))
        queue = MagicMock()
        queue.enqueue_agent_task = AsyncMock(side_effect=enqueue_exc)
        queue.enqueue_worker_task = AsyncMock()
        with patch.object(job_main, "ClaudeDeepResearchRunnerAgent", return_value=agent), \
             patch.object(job_main, "_build_task_queue", return_value=queue), \
             patch.object(job_main.anthropic, "AsyncAnthropic"), \
             patch.object(job_main, "_capture_research_result", AsyncMock()), \
             patch.object(job_main, "_record_billing", AsyncMock()):
            try:
                await job_main._run_job()
                code = None
            except SystemExit as e:
                code = e.code
        return queue, code

    @pytest.mark.slow
    async def test_swallowed_report_enqueue_failure_reports_and_exits_1(self, monkeypatch):
        queue, code = await self._run_real(monkeypatch, RuntimeError("down"))
        assert code == 1
        queue.enqueue_worker_task.assert_awaited_once()
        assert queue.enqueue_worker_task.await_args.args[1]["reason"] == "delivery_failed"

    async def test_success_enqueues_no_failure_notice(self, monkeypatch):
        queue, code = await self._run_real(monkeypatch, None)
        assert code is None
        queue.enqueue_worker_task.assert_not_awaited()


class TestGmailReconnectEnsureFailure:
    async def test_ensure_failure_saves_nothing_and_next_run_notifies(self):
        creds = MagicMock(needs_reconnect=False)
        oauth = AsyncMock()
        oauth.list_for_user.return_value = [creds]
        oauth.update.return_value = True        # the record still exists
        worker, log = _ordered_worker(oauth_credentials=oauth, mail_connector=ConnectorRegistry().get("gmail"))
        worker._agent_factory.ensure_agents_for_user.side_effect = RuntimeError("cold")
        await worker._flag_gmail_reconnect("u1", "a1")
        oauth.update.assert_not_awaited()
        oauth.save.assert_not_awaited()
        assert creds.needs_reconnect is False
        worker._notification.notify.assert_not_awaited()

        async def _ok(_uid):
            log.append("ensure")

        worker._agent_factory.ensure_agents_for_user.side_effect = _ok
        await worker._flag_gmail_reconnect("u1", "a1")
        oauth.update.assert_awaited_once_with(creds)
        oauth.save.assert_not_awaited()          # a flag write never creates a record
        assert creds.needs_reconnect is True
        assert log == ["ensure", "notify"]

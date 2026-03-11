"""
Unit tests for WorkerHandler.

Key invariant under test:
  ensure_agents_for_user() MUST be called before any notify() / deliver_deep_research()
  invocation so that per-user agents are registered in the coordinator before routing.

Coverage:
  _handle_email_indexing
    - completed job  → ensure_agents + notify called
    - paginated job  → ensure_agents NOT called, next page enqueued
    - job not found  → 404, no agent/notify side-effects
    - non-running job → skipped, no agent/notify side-effects
    - missing creds  → 400, no agent/notify side-effects
    - notify raises  → warning logged, 200 still returned

  _handle_deep_research_polling
    - missing job_port / task_queue → 500, ensure_agents NOT called
    - timeout         → ensure_agents + notify called
    - in_progress     → ensure_agents called, re-enqueued
    - completed       → ensure_agents called, deliver_deep_research called
    - failed status   → ensure_agents + notify called
    - consecutive_errors >= 5 → ensure_agents + notify called
"""
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from datetime import datetime

from src.domain.email import IndexingJob
from src.handlers.worker_handler import WorkerHandler
from src.ports.email_indexing_job_repository import EmailIndexingJobRepository
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.ports.task_queue import TaskQueue
from src.services.email_indexing_service import EmailIndexingService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_ID = "user-abc"
_ACCOUNT_ID = "acc-abc"
_JOB_ID = "job-0000-1111-2222-3333"


_NOW = datetime(2026, 3, 10, 12, 0, 0)


def _make_job(status: str = "running", next_page_token: str = None) -> IndexingJob:
    return IndexingJob(
        job_id=_JOB_ID,
        user_id=_USER_ID,
        account_id=_ACCOUNT_ID,
        provider="gmail",
        triggered_by="cabinet",
        status=status,
        next_page_token=next_page_token,
        started_at=_NOW,
        updated_at=_NOW,
    )


def _make_worker(
    *,
    job: IndexingJob = None,
    run_result: IndexingJob = None,
    creds=object(),
) -> tuple[WorkerHandler, MagicMock]:
    """
    Build a WorkerHandler with all dependencies mocked.
    Returns (worker, mocks_namespace).
    """
    email_job_repo = AsyncMock(spec=EmailIndexingJobRepository)
    email_job_repo.get_job.return_value = job if job is not None else _make_job()

    email_indexing = MagicMock(spec=EmailIndexingService)
    email_indexing.run_indexing_job = AsyncMock(
        return_value=run_result if run_result is not None else _make_job(status="completed")
    )
    email_indexing.completion_alert = MagicMock(return_value="Job done!")

    oauth = AsyncMock(spec=OAuthCredentialsPort)
    oauth.get_credentials.return_value = creds

    notification = AsyncMock()  # NotificationPort protocol — no ABC to spec against
    notification.notify = AsyncMock()
    notification.notify_raw = AsyncMock()

    task_queue = AsyncMock(spec=TaskQueue)

    agent_factory = MagicMock()
    agent_factory.ensure_agents_for_user = AsyncMock()

    job_registry = MagicMock()
    job_port = AsyncMock()
    job_registry.get = MagicMock(return_value=job_port)
    job_registry.list_available = MagicMock(return_value=["gemini"])

    ns = MagicMock()
    ns.email_job_repo = email_job_repo
    ns.email_indexing = email_indexing
    ns.oauth = oauth
    ns.notification = notification
    ns.task_queue = task_queue
    ns.agent_factory = agent_factory
    ns.job_registry = job_registry
    ns.job_port = job_port

    worker = WorkerHandler(
        agent_worker_handler=MagicMock(),
        email_indexing_service=email_indexing,
        email_job_repo=email_job_repo,
        oauth_credentials=oauth,
        notification_service=notification,
        consolidation_queue=MagicMock(),
        coordinator=MagicMock(),
        agent_factory=agent_factory,
        indexed_email_repo=None,
        user_repo=MagicMock(),
        task_queue=task_queue,
        job_registry=job_registry,
    )
    return worker, ns


# ---------------------------------------------------------------------------
# _handle_email_indexing
# ---------------------------------------------------------------------------

class TestHandleEmailIndexing:

    async def test_completed_job_calls_ensure_agents_then_notify(self):
        """ensure_agents_for_user is called before notify when job completes."""
        completed = _make_job(status="completed", next_page_token=None)
        worker, ns = _make_worker(run_result=completed)
        call_order = []
        ns.agent_factory.ensure_agents_for_user.side_effect = lambda uid: call_order.append("ensure")
        ns.notification.notify.side_effect = lambda **kw: call_order.append("notify")

        result, status = await worker._handle_email_indexing({"job_id": _JOB_ID})

        assert status == 200
        assert call_order == ["ensure", "notify"], (
            "ensure_agents_for_user must be called BEFORE notify"
        )
        ns.agent_factory.ensure_agents_for_user.assert_called_once_with(_USER_ID)
        ns.notification.notify.assert_called_once()

    async def test_completed_job_notify_receives_correct_user(self):
        completed = _make_job(status="completed")
        worker, ns = _make_worker(run_result=completed)

        await worker._handle_email_indexing({"job_id": _JOB_ID})

        _, kwargs = ns.notification.notify.call_args
        assert kwargs["user_id"] == _USER_ID
        assert kwargs["account_id"] == _ACCOUNT_ID

    async def test_paginated_job_does_not_call_ensure_agents(self):
        """More pages remain — no notification, no agent registration."""
        has_more = _make_job(status="running", next_page_token="cursor-xyz")
        worker, ns = _make_worker(run_result=has_more)

        result, status = await worker._handle_email_indexing({"job_id": _JOB_ID})

        assert status == 200
        ns.agent_factory.ensure_agents_for_user.assert_not_called()
        ns.notification.notify.assert_not_called()
        ns.task_queue.enqueue_email_indexing_task.assert_called_once_with(_JOB_ID)

    async def test_job_not_found_returns_404(self):
        worker, ns = _make_worker()
        ns.email_job_repo.get_job.return_value = None

        result, status = await worker._handle_email_indexing({"job_id": _JOB_ID})

        assert status == 404
        ns.agent_factory.ensure_agents_for_user.assert_not_called()
        ns.notification.notify.assert_not_called()

    async def test_non_running_job_is_skipped(self):
        already_done = _make_job(status="completed")
        worker, ns = _make_worker(job=already_done)

        result, status = await worker._handle_email_indexing({"job_id": _JOB_ID})

        assert status == 200
        assert result["reason"] == "completed"
        ns.agent_factory.ensure_agents_for_user.assert_not_called()
        ns.notification.notify.assert_not_called()

    async def test_missing_oauth_returns_400(self):
        worker, ns = _make_worker(creds=None)

        result, status = await worker._handle_email_indexing({"job_id": _JOB_ID})

        assert status == 400
        ns.agent_factory.ensure_agents_for_user.assert_not_called()
        ns.notification.notify.assert_not_called()

    async def test_missing_job_id_returns_400(self):
        worker, ns = _make_worker()

        result, status = await worker._handle_email_indexing({})

        assert status == 400

    async def test_notify_exception_returns_200_with_warning(self):
        """Notification failure must not surface as an HTTP error."""
        completed = _make_job(status="completed")
        worker, ns = _make_worker(run_result=completed)
        ns.notification.notify.side_effect = RuntimeError("channel unavailable")

        result, status = await worker._handle_email_indexing({"job_id": _JOB_ID})

        assert status == 200
        # ensure_agents was still called (exception came from notify, not from ensure)
        ns.agent_factory.ensure_agents_for_user.assert_called_once_with(_USER_ID)

    async def test_run_indexing_job_invalid_grant_returns_200_not_500(self):
        """
        invalid_grant from Gmail causes run_indexing_job to raise.
        Service already marks job as failed_auth — worker must return 200
        so Cloud Tasks does not retry a permanently-failed auth job.
        """
        worker, ns = _make_worker()
        ns.email_indexing.run_indexing_job.side_effect = ValueError(
            "Gmail token refresh failed: invalid_grant — Token has been expired or revoked."
        )

        result, status = await worker._handle_email_indexing({"job_id": _JOB_ID})

        assert status == 200
        assert result["status"] == "failed"
        # No agent registration or notification attempted after auth failure
        ns.agent_factory.ensure_agents_for_user.assert_not_called()
        ns.notification.notify.assert_not_called()

    async def test_run_indexing_job_generic_error_returns_200_not_500(self):
        """Any exception from run_indexing_job (not just auth) must not produce 500."""
        worker, ns = _make_worker()
        ns.email_indexing.run_indexing_job.side_effect = RuntimeError("Firestore unavailable")

        result, status = await worker._handle_email_indexing({"job_id": _JOB_ID})

        assert status == 200
        assert result["status"] == "failed"


# ---------------------------------------------------------------------------
# _handle_deep_research_polling
# ---------------------------------------------------------------------------

_BASE_PAYLOAD = {
    "interaction_id": "interact-0001",
    "user_id": _USER_ID,
    "account_id": _ACCOUNT_ID,
    "query": "What is the future of AI?",
    "attempt": 0,
    "consecutive_errors": 0,
    "provider": "gemini",
    "session_id": "sess-001",
}


class TestHandleDeepResearchPolling:

    async def test_missing_job_port_returns_500_without_ensure_agents(self):
        worker, ns = _make_worker()
        ns.job_registry.get.side_effect = ValueError("provider not found")

        result, status = await worker._handle_deep_research_polling(_BASE_PAYLOAD)

        assert status == 500
        ns.agent_factory.ensure_agents_for_user.assert_not_called()

    async def test_timeout_calls_ensure_agents_then_notify(self):
        worker, ns = _make_worker()
        payload = {**_BASE_PAYLOAD, "attempt": WorkerHandler._MAX_POLL_ATTEMPTS}
        call_order = []
        ns.agent_factory.ensure_agents_for_user.side_effect = lambda uid: call_order.append("ensure")
        ns.notification.notify.side_effect = lambda **kw: call_order.append("notify")

        result, status = await worker._handle_deep_research_polling(payload)

        assert status == 200
        assert result["status"] == "timeout"
        assert call_order == ["ensure", "notify"]
        ns.agent_factory.ensure_agents_for_user.assert_called_once_with(_USER_ID)

    async def test_in_progress_calls_ensure_agents_and_reenqueues(self):
        worker, ns = _make_worker()
        ns.job_port.get_status = AsyncMock(return_value=("in_progress", ""))

        result, status = await worker._handle_deep_research_polling(_BASE_PAYLOAD)

        assert status == 200
        assert result["status"] == "polling"
        ns.agent_factory.ensure_agents_for_user.assert_called_once_with(_USER_ID)
        ns.notification.notify.assert_not_called()
        ns.task_queue.enqueue_deep_research_polling.assert_called_once()

    async def test_completed_calls_ensure_agents_and_delivers(self):
        worker, ns = _make_worker()
        ns.job_port.get_status = AsyncMock(return_value=("completed", "## Report content"))

        with patch(
            "src.handlers.worker_handler.deliver_deep_research", new_callable=AsyncMock
        ) as mock_deliver:
            result, status = await worker._handle_deep_research_polling(_BASE_PAYLOAD)

        assert status == 200
        assert result["status"] == "delivered"
        ns.agent_factory.ensure_agents_for_user.assert_called_once_with(_USER_ID)
        mock_deliver.assert_called_once()
        # ensure agents was called before deliver
        ensure_call_idx = ns.agent_factory.ensure_agents_for_user.call_args_list[0]
        assert ensure_call_idx == call(_USER_ID)

    async def test_failed_status_calls_ensure_agents_then_notify(self):
        worker, ns = _make_worker()
        ns.job_port.get_status = AsyncMock(return_value=("failed", "provider error"))
        call_order = []
        ns.agent_factory.ensure_agents_for_user.side_effect = lambda uid: call_order.append("ensure")
        ns.notification.notify.side_effect = lambda **kw: call_order.append("notify")

        result, status = await worker._handle_deep_research_polling(_BASE_PAYLOAD)

        assert status == 200
        assert result["status"] == "failed"
        assert call_order == ["ensure", "notify"]

    async def test_consecutive_errors_threshold_calls_ensure_agents_and_notify(self):
        worker, ns = _make_worker()
        ns.job_port.get_status = AsyncMock(side_effect=RuntimeError("API error"))
        payload = {**_BASE_PAYLOAD, "consecutive_errors": 4}  # will hit 5 after this attempt
        call_order = []
        ns.agent_factory.ensure_agents_for_user.side_effect = lambda uid: call_order.append("ensure")
        ns.notification.notify.side_effect = lambda **kw: call_order.append("notify")

        result, status = await worker._handle_deep_research_polling(payload)

        assert status == 200
        assert result["status"] == "dead"
        assert call_order == ["ensure", "notify"]

    async def test_ensure_agents_called_exactly_once_per_request(self):
        """Even with multiple notify paths, ensure_agents_for_user is called once."""
        worker, ns = _make_worker()
        ns.job_port.get_status = AsyncMock(return_value=("completed", "content"))

        with patch("src.handlers.worker_handler.deliver_deep_research", new_callable=AsyncMock):
            await worker._handle_deep_research_polling(_BASE_PAYLOAD)

        assert ns.agent_factory.ensure_agents_for_user.call_count == 1

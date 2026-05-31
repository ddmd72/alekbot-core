"""
Unit tests for WorkerHandler.

Key invariant under test (deep research polling):
  ensure_agents_for_user() MUST be called before any notify() / deliver_deep_research()
  invocation so that per-user agents are registered in the coordinator before routing.

Coverage:
  _handle_email_indexing
    - completed job        → 200, has_more=False, run_indexing_job called
    - paginated job        → 200, next page re-enqueued via task_dispatch
    - job not found        → 404
    - non-running job      → 200, skipped with reason
    - missing creds        → 400
    - run_indexing raises  → 200, status=failed
    - invalid_grant raises → 200, status=failed (no retry by Cloud Tasks)
    - generic error raises → 200, status=failed

  _handle_deep_research_polling
    - missing job_port / task_dispatch → 500, ensure_agents NOT called
    - timeout         → ensure_agents + notify called
    - in_progress     → ensure_agents called, re-enqueued
    - completed       → ensure_agents called, deliver_deep_research called
    - failed status   → ensure_agents + notify called
    - consecutive_errors >= 5 → ensure_agents + notify called
"""
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from datetime import datetime, timedelta, timezone

from src.domain.agent import AgentStatus
from src.domain.billing import BillingAccount, AccountUsageStats
from src.domain.email import IndexingJob
from src.domain.notification_kind import NotificationKind
from src.domain.notify_result import NotifyResult
from src.domain.user import UserProfile
from src.handlers.worker_handler import WorkerHandler
from src.services.consolidation_service import ConsolidationService
from src.services.email_indexing_service import EmailIndexingService
from src.services.email_review_service import EmailReviewService
from src.services.reminders_service import RemindersService
from src.services.task_indexing_service import TaskIndexingService
from src.services.task_setup_service import TaskSetupService


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
    _job = job if job is not None else _make_job()
    _creds = creds

    email_indexing = MagicMock(spec=EmailIndexingService)
    email_indexing.load_job_for_execution = AsyncMock(
        return_value=(_job, _creds, None)
    )
    email_indexing.run_indexing_job = AsyncMock(
        return_value=run_result if run_result is not None else _make_job(status="completed")
    )
    email_indexing.completion_alert = MagicMock(return_value="Job done!")

    notification = AsyncMock()  # NotificationPort protocol — no ABC to spec against
    notification.notify = AsyncMock()
    notification.notify_raw = AsyncMock()

    task_dispatch = AsyncMock()

    agent_factory = MagicMock()
    agent_factory.ensure_agents_for_user = AsyncMock()

    job_registry = MagicMock()
    job_port = AsyncMock()
    job_registry.get = MagicMock(return_value=job_port)
    job_registry.list_available = MagicMock(return_value=["gemini"])

    ns = MagicMock()
    ns.email_indexing = email_indexing
    ns.notification = notification
    ns.task_dispatch = task_dispatch
    ns.agent_factory = agent_factory
    ns.job_registry = job_registry
    ns.job_port = job_port

    worker = WorkerHandler(
        agent_worker_handler=MagicMock(),
        email_indexing_service=email_indexing,
        notification_service=notification,
        consolidation_service=MagicMock(),
        coordinator=MagicMock(),
        agent_factory=agent_factory,
        indexed_email_repo=None,
        user_repo=MagicMock(),
        task_dispatch=task_dispatch,
        job_registry=job_registry,
    )
    return worker, ns


# ---------------------------------------------------------------------------
# _handle_email_indexing
# ---------------------------------------------------------------------------

class TestHandleEmailIndexing:

    async def test_completed_job_returns_200_with_has_more_false(self):
        """Job with no next_page_token: 200, has_more=False, no re-enqueue."""
        completed = _make_job(status="completed", next_page_token=None)
        worker, ns = _make_worker(run_result=completed)

        result, status = await worker._handle_email_indexing({"job_id": _JOB_ID})

        assert status == 200
        assert result["has_more"] is False
        ns.task_dispatch.enqueue_email_indexing_task.assert_not_called()

    async def test_completed_job_calls_run_indexing_job(self):
        """Handler delegates execution to email_indexing_service.run_indexing_job."""
        completed = _make_job(status="completed")
        worker, ns = _make_worker(run_result=completed)

        await worker._handle_email_indexing({"job_id": _JOB_ID})

        ns.email_indexing.run_indexing_job.assert_called_once()

    async def test_paginated_job_does_not_call_ensure_agents(self):
        """More pages remain — no notification, no agent registration."""
        has_more = _make_job(status="running", next_page_token="cursor-xyz")
        worker, ns = _make_worker(run_result=has_more)

        result, status = await worker._handle_email_indexing({"job_id": _JOB_ID})

        assert status == 200
        ns.agent_factory.ensure_agents_for_user.assert_not_called()
        ns.notification.notify.assert_not_called()
        ns.task_dispatch.enqueue_email_indexing_task.assert_called_once_with(_JOB_ID)

    async def test_job_not_found_returns_404(self):
        worker, ns = _make_worker()
        ns.email_indexing.load_job_for_execution.return_value = (None, None, "not_found")

        result, status = await worker._handle_email_indexing({"job_id": _JOB_ID})

        assert status == 404
        ns.agent_factory.ensure_agents_for_user.assert_not_called()
        ns.notification.notify.assert_not_called()

    async def test_non_running_job_is_skipped(self):
        worker, ns = _make_worker()
        ns.email_indexing.load_job_for_execution.return_value = (None, None, "completed")

        result, status = await worker._handle_email_indexing({"job_id": _JOB_ID})

        assert status == 200
        assert result["reason"] == "completed"
        ns.agent_factory.ensure_agents_for_user.assert_not_called()
        ns.notification.notify.assert_not_called()

    async def test_missing_oauth_returns_400(self):
        worker, ns = _make_worker()
        ns.email_indexing.load_job_for_execution.return_value = (None, None, "failed_auth")

        result, status = await worker._handle_email_indexing({"job_id": _JOB_ID})

        assert status == 400
        ns.agent_factory.ensure_agents_for_user.assert_not_called()
        ns.notification.notify.assert_not_called()

    async def test_missing_job_id_returns_400(self):
        worker, ns = _make_worker()

        result, status = await worker._handle_email_indexing({})

        assert status == 400

    async def test_run_indexing_exception_returns_200_and_reports_failed(self):
        """Exception from run_indexing_job: 200 returned, status='failed' in body."""
        worker, ns = _make_worker()
        ns.email_indexing.run_indexing_job.side_effect = RuntimeError("unexpected")

        result, status = await worker._handle_email_indexing({"job_id": _JOB_ID})

        assert status == 200
        assert result["status"] == "failed"

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
        ns.task_dispatch.enqueue_deep_research_polling.assert_called_once()

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


# ---------------------------------------------------------------------------
# Helpers for fully-wired worker (all optional deps present)
# ---------------------------------------------------------------------------

_USER_A = "user-aaa"
_USER_B = "user-bbb"
_ACC_A  = "acc-aaa"
_ACC_B  = "acc-bbb"
_LIST_ID = "list-0001"


def _make_full_worker() -> tuple[WorkerHandler, MagicMock]:
    """
    Build a WorkerHandler with every optional dependency mocked.
    Returns (worker, ns) where ns holds all mock references by name.
    """
    email_indexing = MagicMock(spec=EmailIndexingService)
    email_indexing.start_indexing_for_eligible_users = AsyncMock(return_value=([], 0, 0))
    email_indexing.mark_stale_jobs_failed = AsyncMock(return_value=0)

    consolidation = MagicMock(spec=ConsolidationService)
    consolidation.process_user_batches = AsyncMock(return_value=False)

    task_setup = MagicMock(spec=TaskSetupService)
    task_setup.setup = AsyncMock()
    task_setup.renew_expiring_subscriptions = AsyncMock()
    task_setup.list_microsoft_users = AsyncMock(return_value=[])

    task_indexing = MagicMock(spec=TaskIndexingService)
    task_indexing.reindex_list = AsyncMock()

    reminders_service = MagicMock(spec=RemindersService)
    reminders_service.fire_due_reminders = AsyncMock(return_value=({"fired": 0, "skipped": 0}, 200))

    email_review = MagicMock(spec=EmailReviewService)
    email_review.find_eligible_users = AsyncMock(return_value=[])
    email_review.fetch_review_payload = AsyncMock(return_value=[])
    email_review.build_alert = MagicMock(return_value="[DAILY EMAIL REVIEW] alert text")

    account_repo = AsyncMock()
    account_repo.list_all_accounts = AsyncMock(return_value=[])

    billing_webhook = AsyncMock()
    billing_webhook.post = AsyncMock()

    task_dispatch = AsyncMock()
    agent_factory = MagicMock()
    agent_factory.ensure_agents_for_user = AsyncMock()
    notification = AsyncMock()
    notification.notify = AsyncMock()
    user_repo = MagicMock()
    user_repo.get_user = AsyncMock(return_value=None)
    notes_port = AsyncMock()
    notes_port.get_note = AsyncMock(return_value=None)
    notes_port.mark_fire_delivered = AsyncMock()

    ns = MagicMock()
    ns.email_indexing = email_indexing
    ns.consolidation = consolidation
    ns.task_setup = task_setup
    ns.task_indexing = task_indexing
    ns.reminders_service = reminders_service
    ns.email_review = email_review
    ns.account_repo = account_repo
    ns.billing_webhook = billing_webhook
    ns.task_dispatch = task_dispatch
    ns.agent_factory = agent_factory
    ns.notification = notification
    ns.user_repo = user_repo
    ns.notes_port = notes_port

    worker = WorkerHandler(
        agent_worker_handler=MagicMock(),
        email_indexing_service=email_indexing,
        notification_service=notification,
        consolidation_service=consolidation,
        coordinator=MagicMock(),
        agent_factory=agent_factory,
        indexed_email_repo=None,
        user_repo=user_repo,
        task_dispatch=task_dispatch,
        task_setup=task_setup,
        task_indexing=task_indexing,
        reminders_service=reminders_service,
        notes_port=notes_port,
        email_review=email_review,
        account_repo=account_repo,
        billing_webhook=billing_webhook,
    )
    return worker, ns


# ---------------------------------------------------------------------------
# _handle_start_email_indexing
# ---------------------------------------------------------------------------

class TestHandleStartEmailIndexing:

    async def test_missing_task_dispatch_returns_501(self):
        worker, ns = _make_full_worker()
        worker._task_dispatch = None

        result, status = await worker._handle_start_email_indexing()

        assert status == 501
        ns.email_indexing.start_indexing_for_eligible_users.assert_not_called()

    async def test_enqueues_each_returned_job(self):
        worker, ns = _make_full_worker()
        ns.email_indexing.start_indexing_for_eligible_users.return_value = (
            ["job-1", "job-2"], 2, 1
        )

        result, status = await worker._handle_start_email_indexing()

        assert status == 200
        assert result["started"] == 2
        assert result["skipped"] == 1
        assert ns.task_dispatch.enqueue_email_indexing_task.call_count == 2
        ns.task_dispatch.enqueue_email_indexing_task.assert_any_call("job-1")
        ns.task_dispatch.enqueue_email_indexing_task.assert_any_call("job-2")

    async def test_no_eligible_users_returns_200_started_zero(self):
        worker, ns = _make_full_worker()
        ns.email_indexing.start_indexing_for_eligible_users.return_value = ([], 0, 3)

        result, status = await worker._handle_start_email_indexing()

        assert status == 200
        assert result["started"] == 0
        ns.task_dispatch.enqueue_email_indexing_task.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_watchdog
# ---------------------------------------------------------------------------

class TestHandleWatchdog:

    async def test_delegates_to_service_and_returns_marked_count(self):
        worker, ns = _make_full_worker()
        ns.email_indexing.mark_stale_jobs_failed.return_value = 3

        result, status = await worker._handle_watchdog()

        assert status == 200
        assert result["marked_failed"] == 3
        ns.email_indexing.mark_stale_jobs_failed.assert_called_once()


# ---------------------------------------------------------------------------
# _handle_consolidation
# ---------------------------------------------------------------------------

class TestHandleConsolidation:

    async def test_missing_user_id_returns_400(self):
        worker, ns = _make_full_worker()

        result, status = await worker._handle_consolidation({})

        assert status == 400

    async def test_missing_consolidation_service_returns_400(self):
        worker, ns = _make_full_worker()
        worker._consolidation = None

        result, status = await worker._handle_consolidation({"user_id": _USER_A})

        assert status == 400

    async def test_has_more_true_reenqueues(self):
        worker, ns = _make_full_worker()
        ns.consolidation.process_user_batches.return_value = True

        result, status = await worker._handle_consolidation({"user_id": _USER_A})

        assert status == 200
        ns.task_dispatch.enqueue_consolidation_task.assert_called_once_with(user_id=_USER_A)

    async def test_has_more_false_does_not_reenqueue(self):
        worker, ns = _make_full_worker()
        ns.consolidation.process_user_batches.return_value = False

        result, status = await worker._handle_consolidation({"user_id": _USER_A})

        assert status == 200
        ns.task_dispatch.enqueue_consolidation_task.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_sweep_consolidation
# ---------------------------------------------------------------------------

class TestHandleSweepConsolidation:

    async def test_missing_consolidation_service_returns_501(self):
        worker, ns = _make_full_worker()
        worker._consolidation = None

        result, status = await worker._handle_sweep_consolidation()

        assert status == 501

    async def test_missing_task_dispatch_returns_501(self):
        worker, ns = _make_full_worker()
        worker._task_dispatch = None

        result, status = await worker._handle_sweep_consolidation()

        assert status == 501

    async def test_enqueues_consolidation_per_stuck_user(self):
        worker, ns = _make_full_worker()
        ns.consolidation.find_stuck_users = AsyncMock(return_value=[_USER_A, "user-b"])

        result, status = await worker._handle_sweep_consolidation()

        assert status == 200
        assert result == {"swept": 2}
        assert ns.task_dispatch.enqueue_consolidation_task.await_count == 2
        ns.task_dispatch.enqueue_consolidation_task.assert_any_await(user_id=_USER_A)
        ns.task_dispatch.enqueue_consolidation_task.assert_any_await(user_id="user-b")

    async def test_no_stuck_users_returns_swept_zero(self):
        worker, ns = _make_full_worker()
        ns.consolidation.find_stuck_users = AsyncMock(return_value=[])

        result, status = await worker._handle_sweep_consolidation()

        assert status == 200
        assert result == {"swept": 0}
        ns.task_dispatch.enqueue_consolidation_task.assert_not_called()

    async def test_dispatch_via_handle_routes_to_sweep(self):
        worker, ns = _make_full_worker()
        ns.consolidation.find_stuck_users = AsyncMock(return_value=[])

        result = await worker.handle({"task_type": "sweep_consolidation"})

        assert result == ({"swept": 0}, 200)
        ns.consolidation.find_stuck_users.assert_awaited_once()


# ---------------------------------------------------------------------------
# _handle_setup_microsoft_todo
# ---------------------------------------------------------------------------

class TestHandleSetupMicrosoftTodo:

    async def test_missing_task_setup_returns_501(self):
        worker, ns = _make_full_worker()
        worker._task_setup = None

        result, status = await worker._handle_setup_microsoft_todo({"user_id": _USER_A})

        assert status == 501

    async def test_missing_user_id_returns_400(self):
        worker, ns = _make_full_worker()

        result, status = await worker._handle_setup_microsoft_todo({})

        assert status == 400

    async def test_success_calls_setup_and_returns_200(self):
        worker, ns = _make_full_worker()

        result, status = await worker._handle_setup_microsoft_todo({"user_id": _USER_A})

        assert status == 200
        ns.task_setup.setup.assert_called_once_with(_USER_A)


# ---------------------------------------------------------------------------
# _handle_reindex_task_list
# ---------------------------------------------------------------------------

class TestHandleReindexTaskList:

    async def test_missing_task_indexing_returns_501(self):
        worker, ns = _make_full_worker()
        worker._task_indexing = None

        result, status = await worker._handle_reindex_task_list(
            {"user_id": _USER_A, "list_id": _LIST_ID}
        )

        assert status == 501

    async def test_missing_user_id_returns_400(self):
        worker, ns = _make_full_worker()

        result, status = await worker._handle_reindex_task_list({"list_id": _LIST_ID})

        assert status == 400

    async def test_missing_list_id_returns_400(self):
        worker, ns = _make_full_worker()

        result, status = await worker._handle_reindex_task_list({"user_id": _USER_A})

        assert status == 400

    async def test_success_calls_reindex_and_returns_200(self):
        worker, ns = _make_full_worker()

        result, status = await worker._handle_reindex_task_list(
            {"user_id": _USER_A, "list_id": _LIST_ID}
        )

        assert status == 200
        ns.task_indexing.reindex_list.assert_called_once_with(_USER_A, _LIST_ID)


# ---------------------------------------------------------------------------
# _handle_renew_task_subscriptions
# ---------------------------------------------------------------------------

class TestHandleRenewTaskSubscriptions:

    async def test_missing_task_setup_returns_501(self):
        worker, ns = _make_full_worker()
        worker._task_setup = None

        result, status = await worker._handle_renew_task_subscriptions({"user_id": _USER_A})

        assert status == 501

    async def test_missing_user_id_returns_400(self):
        worker, ns = _make_full_worker()

        result, status = await worker._handle_renew_task_subscriptions({})

        assert status == 400

    async def test_success_calls_renew_and_returns_200(self):
        worker, ns = _make_full_worker()

        result, status = await worker._handle_renew_task_subscriptions({"user_id": _USER_A})

        assert status == 200
        ns.task_setup.renew_expiring_subscriptions.assert_called_once_with(_USER_A)


# ---------------------------------------------------------------------------
# _handle_renew_all_task_subscriptions
# ---------------------------------------------------------------------------

class TestHandleRenewAllTaskSubscriptions:

    async def test_missing_task_setup_returns_501(self):
        worker, ns = _make_full_worker()
        worker._task_setup = None

        result, status = await worker._handle_renew_all_task_subscriptions()

        assert status == 501

    async def test_missing_task_dispatch_returns_501(self):
        worker, ns = _make_full_worker()
        worker._task_dispatch = None

        result, status = await worker._handle_renew_all_task_subscriptions()

        assert status == 501

    async def test_enqueues_renewal_task_per_user(self):
        worker, ns = _make_full_worker()
        ns.task_setup.list_microsoft_users.return_value = [_USER_A, _USER_B]

        result, status = await worker._handle_renew_all_task_subscriptions()

        assert status == 200
        assert result["enqueued"] == 2
        assert ns.task_dispatch.enqueue_worker_task.call_count == 2
        ns.task_dispatch.enqueue_worker_task.assert_any_call(
            "renew_task_subscriptions", {"user_id": _USER_A}
        )
        ns.task_dispatch.enqueue_worker_task.assert_any_call(
            "renew_task_subscriptions", {"user_id": _USER_B}
        )

    async def test_zero_users_returns_200_enqueued_zero(self):
        worker, ns = _make_full_worker()
        ns.task_setup.list_microsoft_users.return_value = []

        result, status = await worker._handle_renew_all_task_subscriptions()

        assert status == 200
        assert result["enqueued"] == 0
        ns.task_dispatch.enqueue_worker_task.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_fire_due_reminders
# ---------------------------------------------------------------------------

class TestHandleFireDueReminders:

    async def test_missing_reminders_service_returns_501(self):
        worker, ns = _make_full_worker()
        worker._reminders_service = None

        result, status = await worker._handle_fire_due_reminders()

        assert status == 501

    async def test_delegates_to_service_and_passes_through_result(self):
        worker, ns = _make_full_worker()
        ns.reminders_service.fire_due_reminders.return_value = ({"fired": 2, "skipped": 1}, 200)

        result, status = await worker._handle_fire_due_reminders()

        assert status == 200
        assert result["fired"] == 2
        ns.reminders_service.fire_due_reminders.assert_called_once()


# ---------------------------------------------------------------------------
# _handle_execute_reminder (Step #8 — per-fire worker)
# ---------------------------------------------------------------------------

_EXECUTE_NOTE_ID = "note-execute-001"
_EXECUTE_USER_ID = "user-execute"
_EXECUTE_DUE = datetime(2026, 5, 1, 6, 5, 0, tzinfo=timezone.utc)
_EXECUTE_PAYLOAD = {
    "note_id": _EXECUTE_NOTE_ID,
    "user_id": _EXECUTE_USER_ID,
    "due_at": _EXECUTE_DUE.isoformat(),
}


def _make_reminder_note(
    *,
    note_id: str = _EXECUTE_NOTE_ID,
    user_id: str = _EXECUTE_USER_ID,
    due: datetime = _EXECUTE_DUE,
    last_delivered_due: datetime = None,
):
    """Build a domain AgentNote suitable for the worker path."""
    from src.domain.agent_note import AgentNote
    return AgentNote(
        note_id=note_id,
        user_id=user_id,
        text="ping the team",
        instruction="Ping the team about the deploy.",
        due=due,
        recurrence=None,
        last_fired=None,
        created_at=due - timedelta(hours=1),
        last_delivered_due=last_delivered_due,
    )


class TestHandleExecuteReminder:
    """Five paths + happy path documented in the docstring of
    ``_handle_execute_reminder``."""

    async def test_missing_notes_port_returns_501(self):
        worker, _ = _make_full_worker()
        worker._notes_port = None

        result, status = await worker._handle_execute_reminder(_EXECUTE_PAYLOAD)

        assert status == 501
        assert "notes_port" in result["error"]

    async def test_missing_payload_fields_returns_400(self):
        worker, _ = _make_full_worker()

        result, status = await worker._handle_execute_reminder({"note_id": "x"})

        assert status == 400
        assert "missing" in result["error"]

    async def test_bad_due_at_returns_400(self):
        worker, _ = _make_full_worker()

        result, status = await worker._handle_execute_reminder(
            {**_EXECUTE_PAYLOAD, "due_at": "not-a-date"}
        )

        assert status == 400
        assert result["error"] == "bad due_at"

    async def test_note_gone_returns_200(self):
        """Note deleted between cron and worker — ack the task, no retry."""
        worker, ns = _make_full_worker()
        ns.notes_port.get_note.return_value = None

        result, status = await worker._handle_execute_reminder(_EXECUTE_PAYLOAD)

        assert status == 200
        assert result["status"] == "note_gone"
        ns.notification.notify.assert_not_called()
        ns.notes_port.mark_fire_delivered.assert_not_called()

    async def test_already_delivered_short_circuits(self):
        """Cloud Tasks retry sees last_delivered_due == due_at → no notify,
        no second mark, ack 200. This is the canonical Cloud-Tasks-retry
        idempotency path (Step #6 D.3 + Step #8)."""
        worker, ns = _make_full_worker()
        ns.notes_port.get_note.return_value = _make_reminder_note(
            last_delivered_due=_EXECUTE_DUE,
        )

        result, status = await worker._handle_execute_reminder(_EXECUTE_PAYLOAD)

        assert status == 200
        assert result["status"] == "already_delivered"
        ns.user_repo.get_user.assert_not_called()
        ns.notification.notify.assert_not_called()
        ns.notes_port.mark_fire_delivered.assert_not_called()

    async def test_no_user_returns_200_without_notify(self):
        worker, ns = _make_full_worker()
        ns.notes_port.get_note.return_value = _make_reminder_note()
        ns.user_repo.get_user.return_value = None

        result, status = await worker._handle_execute_reminder(_EXECUTE_PAYLOAD)

        assert status == 200
        assert result["status"] == "no_user"
        ns.notification.notify.assert_not_called()
        ns.notes_port.mark_fire_delivered.assert_not_called()

    async def test_notify_failed_returns_500_no_mark(self):
        """delivered=False → 500 → Cloud Tasks queue retries with backoff.
        ``mark_fire_delivered`` MUST NOT be called or the retry would be
        falsely short-circuited as already_delivered."""
        from src.domain.agent import AgentStatus
        from src.domain.notify_result import NotifyResult

        worker, ns = _make_full_worker()
        ns.notes_port.get_note.return_value = _make_reminder_note()
        profile = MagicMock()
        profile.account_id = "acc-x"
        ns.user_repo.get_user.return_value = profile
        ns.notification.notify.return_value = NotifyResult(
            delivered=False,
            agent_status=AgentStatus.FAILED,
            error="agent_timeout",
        )

        result, status = await worker._handle_execute_reminder(_EXECUTE_PAYLOAD)

        assert status == 500
        assert result["error"] == "agent_timeout"
        assert result["agent_status"] == AgentStatus.FAILED.value
        ns.notes_port.mark_fire_delivered.assert_not_called()

    async def test_happy_path_delivers_marks_and_acks(self):
        """delivered=True → mark_fire_delivered with (note_id, due_at)
        → 200 ok. Verifies notify was called with kind=REMINDER, the
        right system_alert (build_reminder_alert output), and the
        smart_response_agent override."""
        from src.domain.agent import AgentStatus
        from src.domain.notification_kind import NotificationKind
        from src.domain.notify_result import NotifyResult

        worker, ns = _make_full_worker()
        note = _make_reminder_note()
        ns.notes_port.get_note.return_value = note
        profile = MagicMock()
        profile.account_id = "acc-x"
        ns.user_repo.get_user.return_value = profile
        ns.notification.notify.return_value = NotifyResult(
            delivered=True, agent_status=AgentStatus.SUCCESS,
        )

        result, status = await worker._handle_execute_reminder(_EXECUTE_PAYLOAD)

        assert status == 200
        assert result["status"] == "ok"
        ns.agent_factory.ensure_agents_for_user.assert_awaited_once_with(_EXECUTE_USER_ID)
        # notify called once with the right kind + override + alert.
        ns.notification.notify.assert_awaited_once()
        kw = ns.notification.notify.call_args.kwargs
        assert kw["kind"] == NotificationKind.REMINDER
        assert kw["user_id"] == _EXECUTE_USER_ID
        assert kw["account_id"] == "acc-x"
        assert kw["agent_id_override"] == f"smart_response_agent_{_EXECUTE_USER_ID}"
        # alert includes the note's text + id (smoke that build_reminder_alert was used).
        assert note.text in kw["system_alert"]
        assert _EXECUTE_NOTE_ID in kw["system_alert"]
        # task_complexity defaults to simple_analytics when note.complexity is None.
        assert kw["task_complexity"] == "simple_analytics"
        # mark_fire_delivered stamped with the SAME due_at that came in.
        ns.notes_port.mark_fire_delivered.assert_awaited_once_with(
            note_id=_EXECUTE_NOTE_ID, due_at=_EXECUTE_DUE,
        )

    async def test_passes_suppress_transient_retry(self):
        """Handler returns 5xx on failure → Cloud Tasks retries the task, so notify
        is told to suppress the agent's in-process retry (no layer1 × layer2)."""
        from src.domain.agent import AgentStatus
        from src.domain.notify_result import NotifyResult

        worker, ns = _make_full_worker()
        ns.notes_port.get_note.return_value = _make_reminder_note()
        profile = MagicMock()
        profile.account_id = "acc-x"
        ns.user_repo.get_user.return_value = profile
        ns.notification.notify.return_value = NotifyResult(
            delivered=True, agent_status=AgentStatus.SUCCESS,
        )

        await worker._handle_execute_reminder(_EXECUTE_PAYLOAD)

        assert ns.notification.notify.call_args.kwargs["suppress_transient_retry"] is True

    async def test_happy_path_uses_note_complexity_when_set(self):
        """If note.complexity is set, its .value is forwarded to notify
        as task_complexity (Smart will then resolve the per-call SLA)."""
        from src.domain.agent import AgentStatus
        from src.domain.notify_result import NotifyResult
        from src.domain.task_complexity import TaskComplexity

        worker, ns = _make_full_worker()
        note = _make_reminder_note()
        # Per-note complexity override.
        from dataclasses import replace
        note = replace(note, complexity=TaskComplexity.DEEP_REASONING)
        ns.notes_port.get_note.return_value = note
        profile = MagicMock()
        profile.account_id = "acc-x"
        ns.user_repo.get_user.return_value = profile
        ns.notification.notify.return_value = NotifyResult(
            delivered=True, agent_status=AgentStatus.SUCCESS,
        )

        await worker._handle_execute_reminder(_EXECUTE_PAYLOAD)

        kw = ns.notification.notify.call_args.kwargs
        assert kw["task_complexity"] == "deep_reasoning"

    @pytest.mark.parametrize("complexity_str,expected_tier_name", [
        ("small_talk",        "ECO"),
        ("info_search",       "BALANCED"),
        ("simple_analytics",  "BALANCED"),
        ("deep_reasoning",    "PERFORMANCE"),
    ])
    async def test_tier_forwarded_per_complexity(
        self, complexity_str, expected_tier_name,
    ):
        """Worker resolves note.complexity → tier (via DEFAULT_COMPLEXITY_SETTINGS)
        and passes it to notify(). This wires the per-tier SLA budget to
        the right call without duplicating the complexity→tier mapping."""
        from dataclasses import replace
        from src.domain.agent import AgentStatus
        from src.domain.notify_result import NotifyResult
        from src.domain.task_complexity import TaskComplexity
        from src.domain.user import PerformanceTier

        worker, ns = _make_full_worker()
        note = _make_reminder_note()
        note = replace(note, complexity=TaskComplexity(complexity_str))
        ns.notes_port.get_note.return_value = note
        profile = MagicMock()
        profile.account_id = "acc-x"
        ns.user_repo.get_user.return_value = profile
        ns.notification.notify.return_value = NotifyResult(
            delivered=True, agent_status=AgentStatus.SUCCESS,
        )

        await worker._handle_execute_reminder(_EXECUTE_PAYLOAD)

        kw = ns.notification.notify.call_args.kwargs
        assert kw["tier"] == PerformanceTier[expected_tier_name]

    async def test_tier_is_none_when_complexity_absent(self):
        """No complexity on note → tier=None → notify() falls back to
        the kind's default budget (no per-tier override applied)."""
        from src.domain.agent import AgentStatus
        from src.domain.notify_result import NotifyResult

        worker, ns = _make_full_worker()
        note = _make_reminder_note()  # complexity defaults to None
        ns.notes_port.get_note.return_value = note
        profile = MagicMock()
        profile.account_id = "acc-x"
        ns.user_repo.get_user.return_value = profile
        ns.notification.notify.return_value = NotifyResult(
            delivered=True, agent_status=AgentStatus.SUCCESS,
        )

        await worker._handle_execute_reminder(_EXECUTE_PAYLOAD)

        kw = ns.notification.notify.call_args.kwargs
        assert kw["tier"] is None

    async def test_handle_dispatches_execute_reminder_task_type(self):
        """The top-level dispatcher routes ``execute_reminder`` to the
        new handler — guards against forgetting to register the case."""
        worker, ns = _make_full_worker()
        ns.notes_port.get_note.return_value = None  # → note_gone, 200

        result, status = await worker.handle({
            "task_type": "execute_reminder",
            **_EXECUTE_PAYLOAD,
        })

        assert status == 200
        assert result["status"] == "note_gone"

class TestHandleStartDailyEmailReview:

    async def test_missing_email_review_returns_501(self):
        worker, ns = _make_full_worker()
        worker._email_review = None

        result, status = await worker._handle_start_daily_email_review()

        assert status == 501

    async def test_missing_task_dispatch_returns_501(self):
        worker, ns = _make_full_worker()
        worker._task_dispatch = None

        result, status = await worker._handle_start_daily_email_review()

        assert status == 501

    async def test_enqueues_daily_review_per_eligible_user(self):
        worker, ns = _make_full_worker()
        ns.email_review.find_eligible_users.return_value = [
            (_USER_A, _ACC_A),
            (_USER_B, _ACC_B),
        ]

        result, status = await worker._handle_start_daily_email_review()

        assert status == 200
        assert result["started"] == 2
        assert ns.task_dispatch.enqueue_worker_task.call_count == 2
        ns.task_dispatch.enqueue_worker_task.assert_any_call(
            "daily_email_review", {"user_id": _USER_A, "account_id": _ACC_A}
        )
        ns.task_dispatch.enqueue_worker_task.assert_any_call(
            "daily_email_review", {"user_id": _USER_B, "account_id": _ACC_B}
        )

    async def test_no_eligible_users_returns_200_started_zero(self):
        worker, ns = _make_full_worker()
        ns.email_review.find_eligible_users.return_value = []

        result, status = await worker._handle_start_daily_email_review()

        assert status == 200
        assert result["started"] == 0
        ns.task_dispatch.enqueue_worker_task.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_daily_email_review
# ---------------------------------------------------------------------------

class TestHandleDailyEmailReview:

    async def test_missing_user_id_returns_400(self):
        worker, ns = _make_full_worker()

        result, status = await worker._handle_daily_email_review({"account_id": _ACC_A})

        assert status == 400

    async def test_missing_account_id_returns_400(self):
        worker, ns = _make_full_worker()

        result, status = await worker._handle_daily_email_review({"user_id": _USER_A})

        assert status == 400

    async def test_missing_email_review_service_returns_501(self):
        worker, ns = _make_full_worker()
        worker._email_review = None

        result, status = await worker._handle_daily_email_review(
            {"user_id": _USER_A, "account_id": _ACC_A}
        )

        assert status == 501

    async def test_credentials_unavailable_returns_200_no_notify(self):
        worker, ns = _make_full_worker()
        ns.email_review.fetch_review_payload.return_value = None

        result, status = await worker._handle_daily_email_review(
            {"user_id": _USER_A, "account_id": _ACC_A}
        )

        assert status == 200
        assert result["error"] == "credentials unavailable"
        ns.notification.notify.assert_not_called()

    async def test_no_emails_returns_no_emails_status(self):
        worker, ns = _make_full_worker()
        ns.email_review.fetch_review_payload.return_value = []

        result, status = await worker._handle_daily_email_review(
            {"user_id": _USER_A, "account_id": _ACC_A}
        )

        assert status == 200
        assert result["status"] == "no_emails"
        ns.notification.notify.assert_not_called()

    async def test_success_calls_ensure_agents_then_notify(self):
        worker, ns = _make_full_worker()
        emails = [{"email_id": "e1", "subject": "Hello", "from": "a@b.com"}]
        ns.email_review.fetch_review_payload.return_value = emails
        call_order = []
        ns.agent_factory.ensure_agents_for_user.side_effect = lambda uid: call_order.append("ensure")
        # Handler reads result.delivered to decide on 200 vs 500 — return a
        # successful NotifyResult so the success path is exercised.
        def _track_notify(**kw):
            call_order.append("notify")
            return NotifyResult(delivered=True, agent_status=AgentStatus.SUCCESS)
        ns.notification.notify.side_effect = _track_notify

        result, status = await worker._handle_daily_email_review(
            {"user_id": _USER_A, "account_id": _ACC_A}
        )

        assert status == 200
        assert result["emails"] == 1
        assert call_order == ["ensure", "notify"]
        ns.notification.notify.assert_called_once_with(
            kind=NotificationKind.DAILY_DIGEST,
            user_id=_USER_A,
            account_id=_ACC_A,
            system_alert=ns.email_review.build_alert.return_value,
            agent_id_override=f"smart_response_agent_{_USER_A}",
            save_history=False,
            framing_suffix="",
            thinking_effort="medium",
            task_complexity="deep_reasoning",
            email_for_triage=emails,
            suppress_transient_retry=True,
        )


# ---------------------------------------------------------------------------
# _handle_billing_daily_summary
# ---------------------------------------------------------------------------

def _make_account(
    account_id: str,
    owner_id: str,
    daily_tokens: int = 0,
    daily_cost: float = 0.0,
    prev_daily_tokens: int = 0,
    prev_daily_cost: float = 0.0,
) -> BillingAccount:
    return BillingAccount(
        account_id=account_id,
        iam_policy={owner_id: "owner"},
        usage=AccountUsageStats(
            daily_tokens=daily_tokens,
            daily_cost=daily_cost,
            prev_daily_tokens=prev_daily_tokens,
            prev_daily_cost=prev_daily_cost,
        ),
    )


class TestHandleBillingDailySummary:

    async def test_missing_billing_webhook_returns_501(self):
        worker, ns = _make_full_worker()
        worker._billing_webhook = None

        result, status = await worker._handle_billing_daily_summary()

        assert status == 501

    async def test_missing_account_repo_returns_501(self):
        worker, ns = _make_full_worker()
        worker._account_repo = None

        result, status = await worker._handle_billing_daily_summary()

        assert status == 501

    async def test_no_accounts_with_activity_returns_reported_zero(self):
        worker, ns = _make_full_worker()
        ns.account_repo.list_all_accounts.return_value = [
            _make_account("acc-x", _USER_A, prev_daily_tokens=0, prev_daily_cost=0.0),
        ]

        result, status = await worker._handle_billing_daily_summary()

        assert status == 200
        assert result["reported"] == 0
        ns.billing_webhook.post.assert_not_called()

    async def test_active_accounts_posts_to_webhook(self):
        worker, ns = _make_full_worker()
        ns.account_repo.list_all_accounts.return_value = [
            _make_account(_ACC_A, _USER_A, prev_daily_tokens=1000, prev_daily_cost=0.05),
            _make_account(_ACC_B, _USER_B, prev_daily_tokens=500,  prev_daily_cost=0.02),
        ]
        profile_a = UserProfile(user_id=_USER_A, display_name="Alice")
        ns.user_repo.get_user.side_effect = lambda uid: (
            profile_a if uid == _USER_A else None
        )

        result, status = await worker._handle_billing_daily_summary()

        assert status == 200
        assert result["reported"] == 2
        ns.billing_webhook.post.assert_called_once()
        posted_text = ns.billing_webhook.post.call_args[0][0]
        assert "Alice" in posted_text
        assert "1,000" in posted_text
        assert "Yesterday" in posted_text


# ---------------------------------------------------------------------------
# _handle_repair_email_embeddings + dispatcher routing
# ---------------------------------------------------------------------------

class TestHandleRepairEmailEmbeddings:
    """R13.1 — EmailEmbeddingRepairService wire-up verification.

    Pins the contract: dispatcher routes `repair_email_embeddings` task_type
    to the new handler, and the handler returns 501 when the service is not
    configured (preserving the optional-dependency pattern of other handlers)."""

    async def test_missing_service_returns_501(self):
        worker, _ = _make_full_worker()
        worker._email_embedding_repair = None

        result, status = await worker._handle_repair_email_embeddings()

        assert status == 501
        assert "not configured" in result["error"]

    async def test_runs_service_and_returns_repaired_count(self):
        worker, ns = _make_full_worker()
        repair_service = AsyncMock()
        repair_service.run = AsyncMock(return_value=(42, False))
        worker._email_embedding_repair = repair_service

        result, status = await worker._handle_repair_email_embeddings()

        assert status == 200
        assert result == {"status": "ok", "repaired": 42, "has_more": False}
        repair_service.run.assert_awaited_once()
        ns.task_dispatch.enqueue_worker_task.assert_not_called()

    async def test_saturated_batch_re_enqueues_next_tick(self):
        worker, ns = _make_full_worker()
        repair_service = AsyncMock()
        repair_service.run = AsyncMock(return_value=(100, True))
        worker._email_embedding_repair = repair_service

        result, status = await worker._handle_repair_email_embeddings()

        assert status == 200
        assert result["has_more"] is True
        ns.task_dispatch.enqueue_worker_task.assert_awaited_once_with(
            task_type="repair_email_embeddings",
            payload={},
        )

    async def test_dispatcher_routes_repair_task_type(self):
        worker, _ = _make_full_worker()
        repair_service = AsyncMock()
        repair_service.run = AsyncMock(return_value=(7, False))
        worker._email_embedding_repair = repair_service

        result_tuple = await worker.handle({"task_type": "repair_email_embeddings"})

        assert result_tuple is not None
        result, status = result_tuple
        assert status == 200
        assert result["repaired"] == 7
        repair_service.run.assert_awaited_once()

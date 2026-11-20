"""
Unit tests for WorkerHandler.

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
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from datetime import datetime, timedelta, timezone

from src.composition.worker_setup import build_worker_tasks
from src.domain.email import IndexingJob
from src.domain.user import UserBotConfig
from src.handlers.worker_handler import WorkerHandler
from src.ports.video_generation_port import VideoPollResult
from src.services.background_empty_answer_reporter import BackgroundEmptyAnswerReporter
from src.services.consolidation_service import ConsolidationService
from src.services.email_indexing_service import EmailIndexingService
from src.services.execute_reminder_task import ExecuteReminderTask
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

    video_registry = MagicMock()
    video_port = AsyncMock()
    video_registry.get = MagicMock(return_value=video_port)
    video_registry.list_available = MagicMock(return_value=["grok"])

    link_service = MagicMock()
    short_link_service = AsyncMock()

    ns = MagicMock()
    ns.email_indexing = email_indexing
    ns.notification = notification
    ns.task_dispatch = task_dispatch
    ns.agent_factory = agent_factory
    ns.video_registry = video_registry
    ns.video_port = video_port
    ns.link_service = link_service
    ns.short_link_service = short_link_service

    worker = WorkerHandler(
        agent_worker_handler=MagicMock(),
        email_indexing_service=email_indexing,
        consolidation_service=MagicMock(),
        agent_factory=agent_factory,
        user_repo=MagicMock(),
        task_dispatch=task_dispatch,
        tasks=build_worker_tasks(
            email_review=None, daily_review_marks=None, gmail_reconnect_flag=None, account_repo=None,
            notes_port=None, user_repo=MagicMock(), ensure_agents=agent_factory.ensure_agents_for_user,
            notification_service=notification, alert_sink=None, task_dispatch=task_dispatch,
            video_registry=video_registry, quota_service=None, media_storage=None, link_service=link_service,
            short_link_service=short_link_service),
    )
    return worker, ns


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def worker_handler_factory():
    """
    Factory fixture for creating WorkerHandler instances with customizable services.
    Allows passing optional services (reminders, notes, email review, task dispatch).
    """
    def _factory(
        *,
        reminders_service=None,
        task_dispatch=None,
        **kwargs
    ) -> WorkerHandler:
        """Build a WorkerHandler with specified services, other services mocked."""
        email_indexing = MagicMock(spec=EmailIndexingService)
        notification = AsyncMock()
        agent_factory = MagicMock()
        agent_factory.ensure_agents_for_user = AsyncMock()

        worker = WorkerHandler(
            agent_worker_handler=MagicMock(),
            email_indexing_service=email_indexing,
            consolidation_service=MagicMock(),
            agent_factory=agent_factory,
            user_repo=MagicMock(),
            task_dispatch=task_dispatch,
            reminders_service=reminders_service,
            **kwargs,
        )
        return worker

    return _factory


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
# video_generation_polling — dispatch only; VideoGenerationPollingTask's own tests:
# tests/unit/services/test_video_generation_polling_task.py
# ---------------------------------------------------------------------------

_VIDEO_BASE_PAYLOAD = {
    "task_type": "video_generation_polling",
    "request_id": "req-abc",
    "user_id": "user1",
    "account_id": "acc1",
    "session_id": "user1:C123",
    "attempt": 0,
}


class TestHandleVideoGenerationPolling:

    async def test_dispatcher_routes_video_generation_polling_task_type(self):
        """Guards against forgetting to register the dispatch case."""
        worker, ns = _make_worker()
        ns.video_port.get_status = AsyncMock(return_value=VideoPollResult(status="pending"))

        result = await worker.handle(dict(_VIDEO_BASE_PAYLOAD))

        assert result is not None
        body, status = result
        assert status == 200
        assert body["status"] == "polling"


# ---------------------------------------------------------------------------
# Helpers for fully-wired worker (all optional deps present)
# ---------------------------------------------------------------------------

_USER_A = "user-aaa"
_USER_B = "user-bbb"
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
    ns.billing_webhook = billing_webhook
    ns.task_dispatch = task_dispatch
    ns.agent_factory = agent_factory
    ns.notification = notification
    ns.user_repo = user_repo
    ns.notes_port = notes_port
    ns.execute_reminder = ExecuteReminderTask(
        notes_port=notes_port, user_repo=user_repo, ensure_agents=agent_factory.ensure_agents_for_user,
        notification_service=notification,
        empty_answer_reporter=BackgroundEmptyAnswerReporter(alert_sink=billing_webhook),
    )

    worker = WorkerHandler(
        agent_worker_handler=MagicMock(),
        email_indexing_service=email_indexing,
        consolidation_service=consolidation,
        agent_factory=agent_factory,
        user_repo=user_repo,
        task_dispatch=task_dispatch,
        task_setup=task_setup,
        task_indexing=task_indexing,
        reminders_service=reminders_service,
        tasks={"execute_reminder": ns.execute_reminder},
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

    async def test_sweep_revives_failed_batches_before_enqueueing_each_user(self):
        worker, ns = _make_full_worker()
        ns.consolidation.find_stuck_users = AsyncMock(return_value=[_USER_A, "user-b"])
        order = []
        ns.consolidation.revive_failed_batches = AsyncMock(side_effect=lambda uid: order.append(("revive", uid)))
        ns.task_dispatch.enqueue_consolidation_task = AsyncMock(
            side_effect=lambda user_id: order.append(("enqueue", user_id)))

        await worker._handle_sweep_consolidation()

        assert order == [("revive", _USER_A), ("enqueue", _USER_A), ("revive", "user-b"), ("enqueue", "user-b")]

    async def test_per_task_consolidation_never_revives_failed_batches(self):
        worker, ns = _make_full_worker()
        ns.consolidation.revive_failed_batches = AsyncMock()

        await worker._handle_consolidation({"user_id": _USER_A})

        ns.consolidation.revive_failed_batches.assert_not_called()

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

    @pytest.mark.slow
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
# ExecuteReminderTask (Step #8 — per-fire worker)
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
    """``execute_reminder`` through the handler, and the default complexity -> tier table as the runner reads it.
    Every branch of ``ExecuteReminderTask.__call__``: tests/unit/services/test_execute_reminder_task.py."""

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
        # No overrides configured → the system defaults stand, so the expected tiers
        # below are unchanged by the override merge.
        profile.config = UserBotConfig()
        ns.user_repo.get_user.return_value = profile
        ns.notification.notify.return_value = NotifyResult(
            delivered=True, agent_status=AgentStatus.SUCCESS,
        )

        await ns.execute_reminder(_EXECUTE_PAYLOAD, {})

        kw = ns.notification.notify.call_args.kwargs
        assert kw["tier"] == PerformanceTier[expected_tier_name]

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


# ---------------------------------------------------------------------------
# _handle_companion_consolidation / _handle_sweep_companion_consolidation
# ---------------------------------------------------------------------------

class TestHandleCompanionConsolidation:

    async def test_handle_dispatches_companion_consolidation_task_type(self):
        worker, ns = _make_full_worker()
        worker._companion_extraction = AsyncMock()
        worker._companion_extraction.process_session_batches.return_value = False
        body, status = await worker.handle({"task_type": "companion_consolidation", "session_id": "slack:C1"})
        assert status == 200
        worker._companion_extraction.process_session_batches.assert_called_once_with(
            session_id="slack:C1", max_batches=1,
        )

    async def test_handle_companion_consolidation_missing_session_id(self):
        worker, ns = _make_full_worker()
        worker._companion_extraction = AsyncMock()
        body, status = await worker.handle({"task_type": "companion_consolidation"})
        assert status == 400

    async def test_handle_companion_consolidation_missing_service_returns_400(self):
        worker, ns = _make_full_worker()
        # worker._companion_extraction stays None (WorkerHandler's own default)
        body, status = await worker.handle({"task_type": "companion_consolidation", "session_id": "slack:C1"})
        assert status == 400

    async def test_handle_companion_consolidation_reenqueues_when_has_more(self):
        worker, ns = _make_full_worker()
        worker._companion_extraction = AsyncMock()
        worker._companion_extraction.process_session_batches.return_value = True
        await worker.handle({"task_type": "companion_consolidation", "session_id": "slack:C1"})
        ns.task_dispatch.enqueue_companion_consolidation_task.assert_called_once_with(session_id="slack:C1")

    async def test_dispatch_via_handle_routes_to_sweep_companion_consolidation(self):
        worker, ns = _make_full_worker()
        worker._companion_extraction = AsyncMock()
        worker._companion_extraction.find_stuck_sessions.return_value = ["slack:C1", "slack:C2"]
        body, status = await worker.handle({"task_type": "sweep_companion_consolidation"})
        assert status == 200
        assert body["swept"] == 2
        assert ns.task_dispatch.enqueue_companion_consolidation_task.call_count == 2

    async def test_sweep_companion_consolidation_missing_service_returns_501(self):
        worker, ns = _make_full_worker()
        body, status = await worker.handle({"task_type": "sweep_companion_consolidation"})
        assert status == 501


# ---------------------------------------------------------------------------
# agent_execution — dispatched to AgentWorkerHandler; the redelivery claim is WorkerHandler's
# own (test_worker_handler_task_claim.py), so the delivery is no longer passed through
# ---------------------------------------------------------------------------

async def test_agent_execution_dispatches_payload_to_agent_worker():
    from src.domain.cloud_task_delivery import CloudTaskDelivery

    worker, _ = _make_worker()
    worker._agent_worker.handle_task = AsyncMock(return_value={"status": "success"})
    delivery = CloudTaskDelivery(task_name="t-1", retry_count=0, execution_count=0)
    payload = {"task_type": "agent_execution", "context": {"user_id": _USER_ID}}

    body, status = await worker.handle(payload, delivery=delivery)

    worker._agent_worker.handle_task.assert_awaited_once_with(payload)
    assert body == {"status": "success"}
    assert status == 200

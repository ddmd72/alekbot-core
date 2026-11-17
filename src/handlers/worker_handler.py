"""
Worker Handler
==============

Dispatches Cloud Tasks payloads to the appropriate handler by task_type.
Extracted from main.py for testability and separation of concerns.

Supported task_types:
  - agent_execution          → AgentWorkerHandler
  - email_indexing           → run one indexing page, re-enqueue if more
  - email_indexing_watchdog  → mark stale running jobs as failed
  - consolidation            → process one batch, re-enqueue if more
  - sweep_consolidation      → fan-out cron: re-trigger consolidation for every user with
                                stuck batches (recovers data stalled by e.g. billing errors)
  - companion_consolidation       → process one companion extraction batch, re-enqueue if more
                                     remain (session-keyed analog of consolidation)
  - sweep_companion_consolidation → fan-out cron: re-trigger companion extraction for every
                                     session with stuck batches
  - fire_due_reminders       → cron tick: claim each due note (atomic precondition on `due`)
                                and enqueue per-fire `execute_reminder` Cloud Tasks
  - execute_reminder         → ExecuteReminderTask (services/execute_reminder_task.py), passed in by
                                composition (`tasks`): run one claimed reminder fire
  - setup_microsoft_todo          → TaskSetupService.setup(user_id)
  - reindex_task_list             → TaskIndexingService.reindex_list(user_id, list_id)
  - renew_task_subscriptions      → TaskSetupService.renew_expiring_subscriptions(user_id)
  - renew_all_task_subscriptions  → fan-out: enqueue renew_task_subscriptions for all MS To Do users
  - start_email_indexing          → fan-out: start incremental indexing for all Gmail users with auto_index enabled
  - start_daily_email_review      → fan-out: enqueue daily_email_review for all Gmail users with gmail_daily_review enabled
  - daily_email_review            → fetch last 24h emails, deliver structured payload to SmartAgent for analysis
  - fact_lifecycle_sweep          → the fact janitor (FACT_LIFECYCLE_RFC D4): stale every current fact whose `expires_at`
                                     has passed, unless the account's breaker is tripped; log-only until
                                     `FACT_LIFECYCLE_SWEEP_DRY_RUN=0`. Cron, daily 09:30 Europe/Madrid, not retried; a failed run answers 500
  - fact_review                   → FactReviewService.run(user_id, account_id, mode): the owner's memory review (`daily`, the
                                     default) or only the send of the next question (`next`); another mode is a 400
  - interactive_choice            → InteractiveChoiceService.handle(choice): an owner's pick on an interactive message,
                                     queued by the Slack interactivity route; a payload that is not a choice is a 400
  - start_drive_sync              → fan-out: DriveSyncSchedulerService enqueues one drive_sync tick per
                                     user on the drive queue (the indexer service runs it), raises the
                                     staleness alert and the one reconnect notice (USER_DRIVE_INDEXING_RFC
                                     §7.1, §12.1; RFC Δ5, Δ6)
  - reconnect_notice_sweep        → hourly: IntegrationReconnectService.sweep() sends R1's one notice per Gmail / To Do
                                     whose past connection has no credential (SECRETS_AT_REST_RFC §6.2; Drive's runs in
                                     start_drive_sync); unconfigured answers 200 "disabled"
"""

from __future__ import annotations

import functools
import uuid
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping, Optional, Tuple


if TYPE_CHECKING:
    from ..services.email_review_service import EmailReviewService
    from ..services.task_indexing_service import TaskIndexingService
    from ..services.task_setup_service import TaskSetupService
    from ..ports.account_repository import AccountRepository
    from ..ports.dedup_store import DedupStore
    from ..ports.task_claim_store import TaskClaimStore
    from ..ports.oauth_credentials_port import OAuthCredentialsPort
    from ..services.companion_extraction_service import CompanionExtractionService
    from ..services.drive_sync_scheduler_service import DriveSyncSchedulerService
    from ..services.fact_lifecycle_sweep_service import FactLifecycleSweepService
    from ..services.fact_review_service import FactReviewService
    from ..services.interactive_choice_service import InteractiveChoiceService
    from ..services.integration_reconnect_service import IntegrationReconnectService

from ..domain.cloud_task_delivery import CloudTaskDelivery
from ..domain.connector_spec import ConnectorSpec
from ..domain.exceptions import PROVIDER_OUTAGE_TYPES, OAuthGrantRevokedError
from ..domain.fact_review_task_keys import FACT_REVIEW_TASK, REVIEW_MODES
from ..domain.interactive_choice import INTERACTIVE_CHOICE_TASK, InteractiveChoice
from ..domain.notification_kind import NotificationKind
from ..domain.notify_result import empty_answer_alert, is_empty_answer
from ..domain.worker_task import WorkerTask
from ..infrastructure.notification_sla import dispatch_deadline_s
from ..handlers.agent_worker_handler import AgentWorkerHandler
from ..services.deep_research_delivery import NotificationPort
from ..services.consolidation_service import ConsolidationService
from ..services.email_indexing_service import EmailIndexingService
from ..services.reminders_service import RemindersService
from ..services.task_dispatch_service import TaskDispatchService
from ..utils.logger import logger


# The registry key of the queued Slack event, which carries no task_type; never matched by a sent task_type.
_QUEUED_SLACK_EVENT = "<queued slack event>"


def _payload_task(method: Callable[[dict], Awaitable[Tuple[dict, int]]]) -> WorkerTask:
    @functools.wraps(method)
    async def task(payload: dict, headers: dict) -> Tuple[dict, int]:
        return await method(payload)
    return task


def _no_input_task(method: Callable[[], Awaitable[Tuple[dict, int]]]) -> WorkerTask:
    @functools.wraps(method)
    async def task(payload: dict, headers: dict) -> Tuple[dict, int]:
        return await method()
    return task


class WorkerHandler:
    """
    Dispatches /worker HTTP payloads to specialized handlers.

    Owns no business logic — delegates to existing handlers/services. The only dispatcher of
    /worker: it claims every Cloud Tasks delivery by task name (decisions/worker_task_dedup.md)
    and routes telegram updates and queued Slack events to the adapters attached by build_application_graph.
    """

    def __init__(
        self,
        agent_worker_handler: AgentWorkerHandler,
        email_indexing_service: EmailIndexingService,
        notification_service: NotificationPort,
        consolidation_service: Optional[ConsolidationService],
        agent_factory: Any,  # UserAgentFactory
        user_repo: Any,  # UserRepository
        task_dispatch: Optional[TaskDispatchService] = None,
        task_setup: "Optional[TaskSetupService]" = None,
        task_indexing: "Optional[TaskIndexingService]" = None,
        reminders_service: Optional[RemindersService] = None,
        email_review: "Optional[EmailReviewService]" = None,
        account_repo: "Optional[AccountRepository]" = None,
        billing_webhook: Any = None,  # SlackWebhookAdapter
        companion_extraction: "Optional[CompanionExtractionService]" = None,
        oauth_credentials: "Optional[OAuthCredentialsPort]" = None,
        drive_sync_scheduler: "Optional[DriveSyncSchedulerService]" = None,
        task_claims: "Optional[TaskClaimStore]" = None,
        daily_review_marks: "Optional[DedupStore]" = None,
        fact_lifecycle_sweep: "Optional[FactLifecycleSweepService]" = None,
        fact_review: "Optional[FactReviewService]" = None,
        interactive_choice: "Optional[InteractiveChoiceService]" = None,
        reconnect: "Optional[IntegrationReconnectService]" = None,
        mail_connector: Optional[ConnectorSpec] = None,
        tasks: Mapping[str, WorkerTask] = MappingProxyType({}),
    ) -> None:
        """`tasks`: the runners composition builds outside this handler (`composition/worker_setup.py`), merged over
        the handler's own entries; a task type served by both raises ValueError."""
        self._reconnect = reconnect
        # the mail kind's spec: the Gmail credential `_flag_gmail_reconnect` flags is the one of its kind
        self._mail_connector = mail_connector
        self._fact_lifecycle_sweep = fact_lifecycle_sweep
        self._fact_review = fact_review
        self._interactive_choice = interactive_choice
        self._task_claims = task_claims
        # Keys of attempts running in this process. Check-and-add has no await between, so
        # asyncio's single thread makes it race-free without a lock.
        self._running_claims: set[str] = set()
        self._daily_review_marks = daily_review_marks
        self._slack_worker: Optional[Callable[[dict, dict], Awaitable[Tuple[dict, int]]]] = None
        self._telegram_worker: Optional[Callable[[dict], Awaitable[Tuple[dict, int]]]] = None
        self._agent_worker = agent_worker_handler
        self._email_indexing = email_indexing_service
        self._notification = notification_service
        self._consolidation = consolidation_service
        self._agent_factory = agent_factory
        self._user_repo = user_repo
        self._task_dispatch = task_dispatch
        self._task_setup = task_setup
        self._task_indexing = task_indexing
        self._reminders_service = reminders_service
        self._email_review = email_review
        self._account_repo = account_repo
        self._billing_webhook = billing_webhook
        self._companion_extraction = companion_extraction
        self._oauth_credentials = oauth_credentials
        self._drive_sync_scheduler = drive_sync_scheduler
        own: dict[str, WorkerTask] = {
            "agent_execution": _payload_task(self._handle_agent_execution),
            "telegram_update": _payload_task(self._handle_telegram_update),
            "email_indexing": _payload_task(self._handle_email_indexing),
            "email_indexing_watchdog": _no_input_task(self._handle_watchdog),
            "consolidation": _payload_task(self._handle_consolidation),
            "sweep_consolidation": _no_input_task(self._handle_sweep_consolidation),
            "companion_consolidation": _payload_task(self._handle_companion_consolidation),
            "sweep_companion_consolidation": _no_input_task(self._handle_sweep_companion_consolidation),
            "setup_microsoft_todo": _payload_task(self._handle_setup_microsoft_todo),
            "reindex_task_list": _payload_task(self._handle_reindex_task_list),
            "renew_task_subscriptions": _payload_task(self._handle_renew_task_subscriptions),
            "renew_all_task_subscriptions": _no_input_task(self._handle_renew_all_task_subscriptions),
            "fire_due_reminders": _no_input_task(self._handle_fire_due_reminders),
            "start_email_indexing": _no_input_task(self._handle_start_email_indexing),
            "start_daily_email_review": _no_input_task(self._handle_start_daily_email_review),
            "daily_email_review": _payload_task(self._handle_daily_email_review),
            "billing_daily_summary": _no_input_task(self._handle_billing_daily_summary),
            "start_drive_sync": _no_input_task(self._handle_start_drive_sync),
            "reconnect_notice_sweep": _no_input_task(self._handle_reconnect_notice_sweep),
            "fact_lifecycle_sweep": _no_input_task(self._handle_fact_lifecycle_sweep),
            FACT_REVIEW_TASK: _payload_task(self._handle_fact_review),
            INTERACTIVE_CHOICE_TASK: _payload_task(self._handle_interactive_choice),
            _QUEUED_SLACK_EVENT: self._handle_queued_slack_event,
        }
        served_twice = own.keys() & tasks.keys()
        if served_twice:
            raise ValueError(f"WorkerHandler: task types passed in that it already serves: {sorted(served_twice)}")
        self._tasks: Mapping[str, WorkerTask] = {**own, **tasks}

    def task_types(self) -> frozenset[str]:
        """The task_type values this handler dispatches (the queued Slack event carries none)."""
        return frozenset(self._tasks) - {_QUEUED_SLACK_EVENT}

    def attach_slack_worker(
        self, fn: Callable[[dict, dict], Awaitable[Tuple[dict, int]]],
    ) -> None:
        """Late wiring: the Slack adapter is built after this handler."""
        self._slack_worker = fn

    def attach_telegram_worker(
        self, fn: Callable[[dict], Awaitable[Tuple[dict, int]]],
    ) -> None:
        """Late wiring: the Telegram adapter is built after this handler (and may be absent)."""
        self._telegram_worker = fn

    async def handle(
        self,
        payload: dict,
        delivery: Optional[CloudTaskDelivery] = None,
        headers: Optional[dict] = None,
    ) -> Tuple[dict, int]:
        """
        Claim the delivery, dispatch by task_type, release the claim when the attempt
        should be retried.

        ``delivery`` — the Cloud Tasks headers of this request (None outside Cloud Tasks:
        scheduler, manual curl); a second delivery of a claimed task is acked as duplicate.
        ``headers`` — the request headers, passed to the queued Slack event worker.

        Always returns (body_dict, status_code).
        """
        key = delivery.task_name if delivery is not None and self._task_claims is not None else None
        if key is not None:
            # Another attempt of this task is live in this process: a true duplicate, no store call.
            if key in self._running_claims:
                logger.info("[WorkerHandler] Skipping concurrent delivery: task=%s", key)
                return {"status": "duplicate"}, 200
            self._running_claims.add(key)  # before any await: closes the check-then-add window
        try:
            return await self._run_claimed(payload, delivery, headers or {}, key is not None)
        finally:
            # Only the attempt that added the key removes it, and only after mark_done/release finished.
            if key is not None:
                self._running_claims.discard(key)

    async def _run_claimed(
        self, payload: dict, delivery: Optional[CloudTaskDelivery], headers: dict, claimable: bool
    ) -> Tuple[dict, int]:
        attempt = uuid.uuid4().hex
        if claimable and not await self._claim(delivery, payload, attempt):
            return {"status": "duplicate"}, 200
        try:
            body, status = await self._dispatch(payload, headers)
        except BaseException:
            await self._release(delivery, attempt)
            raise
        # 5xx and 429 ask Cloud Tasks to retry: the retry must not be skipped as a duplicate.
        if status >= 500 or status == 429:
            await self._release(delivery, attempt)
        else:
            await self._mark_done(delivery, attempt)
        return body, status

    async def _claim(self, delivery: Optional[CloudTaskDelivery], payload: dict, attempt: str) -> bool:
        """False only when a finished attempt holds the claim; any other running claim is taken over
        as a dead attempt (single instance: decisions/worker_task_dedup.md).
        Fails open: a store error runs the task — a duplicate beats a lost task."""
        if delivery is None or self._task_claims is None:
            return True
        key = delivery.task_name
        try:
            claimed = await self._task_claims.try_claim(key, attempt)
        except Exception as exc:
            logger.error(
                "[WorkerHandler] task claim failed, running anyway: task=%s error=%s",
                key, exc, exc_info=True,
            )
            return True
        if not claimed:
            logger.info(
                "[WorkerHandler] Skipping redelivered task: task=%s task_type=%s "
                "retry_count=%s execution_count=%s",
                key, payload.get("task_type"),
                delivery.retry_count, delivery.execution_count,
            )
        return claimed

    async def _mark_done(self, delivery: Optional[CloudTaskDelivery], attempt: str) -> None:
        if delivery is None or self._task_claims is None:
            return
        try:
            await self._task_claims.mark_done(delivery.task_name, attempt)
        except Exception as exc:
            logger.error("[WorkerHandler] claim mark_done failed: task=%s error=%s",
                         delivery.task_name, exc, exc_info=True)

    async def _release(self, delivery: Optional[CloudTaskDelivery], attempt: str) -> None:
        if delivery is None or self._task_claims is None:
            return
        try:
            await self._task_claims.release(delivery.task_name, attempt)
        except Exception as exc:
            logger.error(
                "[WorkerHandler] claim release failed — the retry takes the claim over: "
                "task=%s error=%s", delivery.task_name, exc, exc_info=True,
            )

    async def _dispatch(self, payload: dict, headers: dict) -> Tuple[dict, int]:
        task_type = payload.get("task_type")
        if task_type is None and "event" in payload and "session_id" in payload:
            # Queued Slack event (GcpTaskQueue.enqueue_slack_event): no task_type by design.
            task: Optional[WorkerTask] = self._tasks[_QUEUED_SLACK_EVENT]
        elif isinstance(task_type, str) and task_type != _QUEUED_SLACK_EVENT:
            task = self._tasks.get(task_type)
        else:
            task = None
        if task is None:
            logger.error("[WorkerHandler] unknown task_type=%r", task_type)
            return {"error": "unknown task_type"}, 400
        return await task(payload, headers)

    async def _handle_agent_execution(self, payload: dict) -> Tuple[dict, int]:
        user_id = payload.get("context", {}).get("user_id", "")
        if user_id:
            await self._agent_factory.ensure_agents_for_user(user_id)
        result = await self._agent_worker.handle_task(payload)
        return result, 200

    async def _handle_telegram_update(self, payload: dict) -> Tuple[dict, int]:
        if self._telegram_worker is None:
            logger.error("[WorkerHandler] telegram_update delivered but Telegram is not configured")
            return {"error": "telegram not configured"}, 503
        return await self._telegram_worker(payload)

    async def _handle_queued_slack_event(self, payload: dict, headers: dict) -> Tuple[dict, int]:
        if self._slack_worker is None:
            logger.error("[WorkerHandler] Slack event delivered but no Slack worker is attached")
            return {"error": "slack not configured"}, 503
        return await self._slack_worker(payload, headers)

    # ------------------------------------------------------------------
    # Email indexing
    # ------------------------------------------------------------------

    async def _handle_start_email_indexing(self) -> Tuple[dict, int]:
        """
        Fan-out: start incremental indexing for all eligible Gmail users.
        Delegates eligibility check and job creation to EmailIndexingService.
        Called by Cloud Scheduler hourly.
        """
        if not self._task_dispatch:
            logger.warning("[Worker] start_email_indexing: task_queue not configured")
            return {"error": "task_queue not configured"}, 501

        now_utc = datetime.now(timezone.utc)
        job_ids, started, skipped = await self._email_indexing.start_indexing_for_eligible_users(
            self._user_repo, now_utc
        )
        for job_id in job_ids:
            await self._task_dispatch.enqueue_email_indexing_task(job_id)
            logger.info(f"[Worker] start_email_indexing: enqueued job {job_id[:8]}")

        logger.info(f"[Worker] start_email_indexing complete: started={started}, skipped={skipped}")
        return {"started": started, "skipped": skipped}, 200

    async def _handle_email_indexing(self, payload: dict) -> Tuple[dict, int]:
        """
        Process one page of email indexing. Re-enqueues if more pages remain.
        Delegates job/credential loading to EmailIndexingService.
        """
        job_id = payload.get("job_id")
        logger.info(f"📧 [Worker] email_indexing received: job_id={job_id}")
        if not job_id:
            return {"error": "missing job_id"}, 400

        job, creds, skip_reason = await self._email_indexing.load_job_for_execution(job_id)
        if skip_reason:
            if skip_reason == "not_found":
                return {"error": "job not found"}, 404
            if skip_reason == "failed_auth":
                return {"error": "oauth credentials missing"}, 400
            return {"status": "skipped", "reason": skip_reason}, 200
        try:
            job = await self._email_indexing.run_indexing_job(
                job=job,
                credentials=creds,
                account_id=job.account_id,
                max_pages=1,
                mode=job.mode,
                backfill_until=job.backfill_until,
            )
        except OAuthGrantRevokedError as exc:
            # Job is already failed_auth (set by the service). Retrying cannot help.
            logger.warning(f"⚠️ [Worker] Indexing job {job_id[:8]}: Gmail grant revoked: {exc}")
            await self._flag_gmail_reconnect(job.user_id, job.account_id)
            return {"status": "failed", "error": "gmail_reconnect_required"}, 200
        except PROVIDER_OUTAGE_TYPES as exc:
            # The job is still `running` and the page is not stored: a 503 releases the claim and Cloud Tasks retries.
            logger.warning(f"⚠️ [Worker] Indexing job {job_id[:8]}: model provider unavailable, retry requested: {exc}")
            return {"status": "retry", "error": "model_provider_unavailable"}, 503
        except Exception as exc:
            logger.warning(f"⚠️ [Worker] Indexing job {job_id[:8]} raised (status updated by service): {exc}")
            return {"status": "failed", "error": str(exc)}, 200

        if job.next_page_token and self._task_dispatch:
            await self._task_dispatch.enqueue_email_indexing_task(job.job_id)
            logger.info(f"📬 [Worker] Re-enqueued email indexing page for job {job.job_id[:8]}")
        elif not job.next_page_token:
            logger.info(f"✅ [Worker] Indexing job {job_id[:8]} complete: stored={job.emails_stored}, failed={job.emails_failed}")

        return {"status": "ok", "has_more": bool(job.next_page_token)}, 200

    # ------------------------------------------------------------------
    # Watchdog
    # ------------------------------------------------------------------

    async def _handle_watchdog(self) -> Tuple[dict, int]:
        """Mark stale 'running' jobs as failed. Delegates to EmailIndexingService."""
        stale_threshold = datetime.now(timezone.utc) - timedelta(hours=2)
        marked = await self._email_indexing.mark_stale_jobs_failed(stale_threshold)
        return {"status": "ok", "marked_failed": marked}, 200

    # ------------------------------------------------------------------
    # Consolidation
    # ------------------------------------------------------------------

    async def _handle_consolidation(self, payload: dict) -> Tuple[dict, int]:
        """
        Process one consolidation batch. Re-enqueues if more remain.
        One batch per HTTP request → each Cloud Task gets full CPU on Cloud Run.
        """
        user_id = payload.get("user_id")
        if not user_id or self._consolidation is None:
            return {"error": "missing user_id or consolidation service not ready"}, 400

        has_more = await self._consolidation.process_user_batches(
            user_id=user_id,
            max_batches=1,
        )
        if has_more and self._task_dispatch:
            await self._task_dispatch.enqueue_consolidation_task(user_id=user_id)
            logger.info(f"📬 [Worker] Re-enqueued next consolidation task for user {user_id[:8]}")
        return {"status": "ok"}, 200

    async def _handle_sweep_consolidation(self) -> Tuple[dict, int]:
        """
        Fan-out cron: re-trigger consolidation for every user with stuck batches.

        A batch is "stuck" if it stalled (e.g. provider billing exhaustion) after its
        re-enqueue chain stopped — its data was extracted from session history but never
        written to memory, and without this sweep it would wait for the next session
        overflow (potentially days). Called by Cloud Scheduler hourly. Per stuck user we
        revive its FAILED batches (bounded) and enqueue the normal consolidation task.
        """
        if self._consolidation is None or self._task_dispatch is None:
            logger.warning("[Worker] sweep_consolidation: services not configured")
            return {"error": "services not configured"}, 501

        user_ids = await self._consolidation.find_stuck_users()
        for user_id in user_ids:
            # The only place a FAILED batch is revived (bounded by its lifetime `revivals`).
            await self._consolidation.revive_failed_batches(user_id)
            await self._task_dispatch.enqueue_consolidation_task(user_id=user_id)
            logger.info(f"[Worker] sweep_consolidation: enqueued for {user_id[:8]}")

        logger.info(f"[Worker] sweep_consolidation complete: swept={len(user_ids)}")
        return {"swept": len(user_ids)}, 200

    # ------------------------------------------------------------------
    # Companion extraction (RFC docs/10_rfcs/COMPANION_AGENTS_RFC.md §6)
    # ------------------------------------------------------------------

    async def _handle_companion_consolidation(self, payload: dict) -> Tuple[dict, int]:
        """
        Process one companion extraction batch. Re-enqueues if more remain.
        Mirrors _handle_consolidation, session-keyed instead of user-keyed.
        """
        session_id = payload.get("session_id")
        if not session_id or self._companion_extraction is None:
            return {"error": "missing session_id or companion_extraction service not ready"}, 400

        has_more = await self._companion_extraction.process_session_batches(
            session_id=session_id, max_batches=1,
        )
        if has_more and self._task_dispatch:
            await self._task_dispatch.enqueue_companion_consolidation_task(session_id=session_id)
            logger.info(f"📬 [Worker] Re-enqueued next companion_consolidation task for session {session_id[:12]}")
        return {"status": "ok"}, 200

    async def _handle_sweep_companion_consolidation(self) -> Tuple[dict, int]:
        """
        Fan-out cron: re-trigger extraction for every session with stuck batches.
        Mirrors _handle_sweep_consolidation. Scheduled hourly (:47) by the Cloud Scheduler
        job alek-bot-dev-sweep-companion-consolidation.
        """
        if self._companion_extraction is None or self._task_dispatch is None:
            logger.warning("[Worker] sweep_companion_consolidation: services not configured")
            return {"error": "services not configured"}, 501

        session_ids = await self._companion_extraction.find_stuck_sessions()
        for session_id in session_ids:
            await self._task_dispatch.enqueue_companion_consolidation_task(session_id=session_id)
            logger.info(f"[Worker] sweep_companion_consolidation: enqueued for {session_id[:12]}")

        logger.info(f"[Worker] sweep_companion_consolidation complete: swept={len(session_ids)}")
        return {"swept": len(session_ids)}, 200

    async def _empty_answer_final(
        self, path: str, user_id: str, note_id: Optional[str] = None,
    ) -> None:
        """A background turn ended with no answer: final, never retried (one ERROR line + one ops alert).

        No content in either; the caller then takes its success path and answers 200."""
        logger.error(
            "[Worker] background_empty_answer path=%s note=%s user=%s",
            path, note_id, user_id[:8],
            extra={"event": "background_empty_answer", "path": path, "note_id": note_id,
                   "user_id": user_id[:8]},
        )
        if self._billing_webhook is None:
            return
        try:
            await self._billing_webhook.post(empty_answer_alert(path, user_id, note_id))
        except Exception as exc:
            logger.warning("[Worker] background_empty_answer alert failed: %s", exc)

    # ------------------------------------------------------------------
    # MS To Do task handlers
    # ------------------------------------------------------------------

    async def _handle_setup_microsoft_todo(self, payload: dict) -> Tuple[dict, int]:
        """Idempotent onboarding: ensure primary list + subscriptions + enqueue reindex."""
        if not self._task_setup:
            logger.warning("[Worker] setup_microsoft_todo: task_setup not configured")
            return {"error": "task_setup not configured"}, 501
        user_id = payload.get("user_id", "")
        if not user_id:
            return {"error": "missing user_id"}, 400
        await self._task_setup.setup(user_id)
        logger.info(f"[Worker] setup_microsoft_todo complete: user={user_id[:8]}")
        return {"status": "ok"}, 200

    async def _handle_reindex_task_list(self, payload: dict) -> Tuple[dict, int]:
        """Fetch all tasks from one list, embed, and upsert into search index."""
        if not self._task_indexing:
            logger.warning("[Worker] reindex_task_list: task_indexing not configured")
            return {"error": "task_indexing not configured"}, 501
        user_id = payload.get("user_id", "")
        list_id = payload.get("list_id", "")
        if not user_id or not list_id:
            return {"error": "missing user_id or list_id"}, 400
        await self._task_indexing.reindex_list(user_id, list_id)
        logger.info(f"[Worker] reindex_task_list complete: user={user_id[:8]}, list={list_id[:8]}")
        return {"status": "ok"}, 200

    async def _handle_renew_task_subscriptions(self, payload: dict) -> Tuple[dict, int]:
        """Secondary defense: renew all subscriptions expiring within 24h."""
        if not self._task_setup:
            logger.warning("[Worker] renew_task_subscriptions: task_setup not configured")
            return {"error": "task_setup not configured"}, 501
        user_id = payload.get("user_id", "")
        if not user_id:
            return {"error": "missing user_id"}, 400
        await self._task_setup.renew_expiring_subscriptions(user_id)
        logger.info(f"[Worker] renew_task_subscriptions complete: user={user_id[:8]}")
        return {"status": "ok"}, 200

    async def _handle_renew_all_task_subscriptions(self) -> Tuple[dict, int]:
        """Daily fan-out: enqueue renew_task_subscriptions for every MS To Do user."""
        if not self._task_setup or not self._task_dispatch:
            logger.warning("[Worker] renew_all_task_subscriptions: task_setup or task_queue not configured")
            return {"error": "task_setup not configured"}, 501
        user_ids = await self._task_setup.list_microsoft_users()
        for user_id in user_ids:
            await self._task_dispatch.enqueue_worker_task(
                "renew_task_subscriptions", {"user_id": user_id}
            )
        logger.info(f"[Worker] renew_all_task_subscriptions: enqueued {len(user_ids)} renewal tasks")
        return {"status": "ok", "enqueued": len(user_ids)}, 200

    # ------------------------------------------------------------------
    # R1 reconnect notices (SECRETS_AT_REST_RFC §6.2)
    # ------------------------------------------------------------------

    async def _handle_reconnect_notice_sweep(self) -> Tuple[dict, int]:
        """Cloud Scheduler, hourly: R1's one notice per Gmail / To Do whose past connection has no credential.

        Unconfigured answers 200 like start_drive_sync: a 5xx request log is ERROR severity, which the error alert policy
        would page on every hour."""
        if not self._reconnect:
            logger.info("[Worker] reconnect_notice_sweep: not configured; skipped")
            return {"status": "disabled"}, 200
        return await self._reconnect.sweep(), 200

    # ------------------------------------------------------------------
    # Drive index fan-out
    # ------------------------------------------------------------------

    async def _handle_start_drive_sync(self) -> Tuple[dict, int]:
        """Cloud Scheduler, every TICK_INTERVAL: one drive_sync tick per user (delegates, RFC Δ5).

        Unconfigured (no DRIVE_INDEXER_URL, e.g. the first pass of the two-pass first deploy) answers
        200, not the 501 other fan-outs use: the job fires every 10 minutes, and a 5xx request log is
        ERROR severity, which the error alert policy would page on until the second pass."""
        if not self._drive_sync_scheduler:
            logger.info("[Worker] start_drive_sync: drive indexing not configured; skipped")
            return {"status": "disabled"}, 200
        return await self._drive_sync_scheduler.fan_out(), 200

    # ------------------------------------------------------------------
    # Self-reminders: fire_due_reminders
    # ------------------------------------------------------------------

    async def _handle_fire_due_reminders(self) -> Tuple[dict, int]:
        """
        Fire all reminders with due <= now.
        Delegates to RemindersService which owns the AgentNotePort interaction.
        Called by Cloud Scheduler every 15 minutes.
        """
        if not self._reminders_service:
            logger.warning("[Worker] fire_due_reminders: reminders_service not configured")
            return {"error": "reminders_service not configured"}, 501
        return await self._reminders_service.fire_due_reminders()

    # ------------------------------------------------------------------
    # Daily email review
    # ------------------------------------------------------------------

    async def _flag_gmail_reconnect(self, user_id: str, account_id: str) -> None:
        """Google rejected the user's Gmail refresh token: mark the credentials
        `needs_reconnect` (Cabinet shows it) and tell the user once per breakage.

        Only the transition notifies — the daily review and the auto-index keep
        hitting the dead token every day until the user reconnects, and that must
        not become a daily message. A reconnect saves fresh credentials, which
        clears the flag, so the next breakage notifies again. Best-effort: a
        failure here is logged, never raised into the task.
        """
        if not self._oauth_credentials or self._mail_connector is None:
            logger.warning("[Worker] Gmail grant revoked but oauth_credentials not configured")
            return
        try:
            keys = await self._oauth_credentials.list_keys(user_id, self._mail_connector.kind)
            creds = await self._oauth_credentials.get(user_id, keys[0].credential_id) if keys else None
            if creds is None or creds.needs_reconnect:
                return
            # Before the save: a failure here must leave the flag down so the next run retries the notice.
            await self._agent_factory.ensure_agents_for_user(user_id)
            creds.needs_reconnect = True
            if not await self._oauth_credentials.update(creds):
                logger.info(f"[Worker] Gmail disconnected meanwhile for {user_id[:8]}, nothing to flag")
                return
            logger.warning(f"[Worker] Gmail marked needs_reconnect for {user_id[:8]}")
            await self._notification.notify(
                kind=NotificationKind.DOCUMENT_DELIVERY,
                user_id=user_id,
                account_id=account_id,
                system_alert=(
                    "Google revoked Alek's access to the user's Gmail (refresh token "
                    "rejected — usually a Google password change or access removed in "
                    "the Google account settings). Until the user reconnects, the daily "
                    "email review and email auto-indexing do not run. Tell them, and "
                    "that the fix is Cabinet → Gmail → Reconnect."
                ),
            )
        except Exception as exc:
            logger.error(f"[Worker] Gmail reconnect flag/notice failed for {user_id[:8]}: {exc}", exc_info=True)

    async def _handle_start_daily_email_review(self) -> Tuple[dict, int]:
        """
        Fan-out: enqueue daily_email_review once per user per local date. Called hourly;
        a user is eligible during the first 3 hours from gmail_daily_review_hour and the
        (user, slot date) mark makes every later tick in the window a no-op.
        """
        if not self._email_review or not self._task_dispatch:
            logger.warning("[Worker] start_daily_email_review: required services not configured")
            return {"error": "services not configured"}, 501

        now_utc = datetime.now(timezone.utc)
        eligible = await self._email_review.find_eligible_users(self._user_repo, now_utc)

        marks = self._daily_review_marks
        if marks is None:
            logger.warning("[Worker] start_daily_email_review: no daily_review_marks store, duplicates possible")

        started = 0
        failed = 0
        for user in eligible:
            mark_key = f"daily_review:{user.user_id}:{user.slot_date.isoformat()}"
            if marks is not None:
                try:
                    claimed = await marks.try_mark_processed(mark_key)
                except Exception as exc:
                    # A double review is worse than a one-hour delay: retried next tick.
                    logger.error(
                        f"[Worker] start_daily_email_review: claim failed for {user.user_id[:8]}: {exc}",
                        exc_info=True,
                    )
                    failed += 1
                    continue
                if not claimed:
                    logger.info(
                        f"[Worker] start_daily_email_review: already started for "
                        f"{user.user_id[:8]} on {user.slot_date}"
                    )
                    continue
            try:
                await self._task_dispatch.enqueue_worker_task(
                    "daily_email_review",
                    {"user_id": user.user_id, "account_id": user.account_id},
                    deadline_seconds=dispatch_deadline_s(NotificationKind.DAILY_DIGEST),
                    # A queue call that errored after creating the task cannot yield a second task.
                    dedup_key=mark_key,
                )
            except Exception as exc:
                logger.error(
                    f"[Worker] start_daily_email_review: enqueue failed for {user.user_id[:8]}: {exc}",
                    exc_info=True,
                )
                failed += 1
                if marks is not None:
                    try:
                        await marks.release(mark_key)
                    except Exception as rel_exc:
                        logger.error(
                            f"[Worker] start_daily_email_review: mark release failed for "
                            f"{user.user_id[:8]}: {rel_exc}",
                            exc_info=True,
                        )
                continue
            started += 1
            logger.info(f"[Worker] start_daily_email_review: enqueued for {user.user_id[:8]}")

        logger.info(f"[Worker] start_daily_email_review complete: started={started} failed={failed}")
        return {"started": started, "failed": failed}, 200

    async def _handle_daily_email_review(self, payload: dict) -> Tuple[dict, int]:
        """
        Fetch last 24h emails for a user and deliver to SmartAgent for analysis.
        SmartAgent produces an HTML page via create_html_page.
        """
        user_id = payload.get("user_id")
        account_id = payload.get("account_id")
        if not user_id or not account_id:
            return {"error": "missing user_id or account_id"}, 400

        if not self._email_review:
            logger.warning("[Worker] daily_email_review: email_review_service not configured")
            return {"error": "email_review_service not configured"}, 501

        try:
            emails = await self._email_review.fetch_review_payload(user_id)
        except OAuthGrantRevokedError:
            await self._flag_gmail_reconnect(user_id, account_id)
            return {"error": "gmail_reconnect_required"}, 200
        if emails is None:
            return {"error": "credentials unavailable"}, 200
        if not emails:
            logger.info(f"[Worker] daily_email_review: no emails in last 24h for {user_id[:8]}")
            return {"status": "no_emails"}, 200

        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        system_alert = self._email_review.build_alert(date_str, len(emails))

        await self._agent_factory.ensure_agents_for_user(user_id)
        result = await self._notification.notify(
            kind=NotificationKind.DAILY_DIGEST,
            user_id=user_id,
            account_id=account_id,
            system_alert=system_alert,
            agent_id_override=f"smart_response_agent_{user_id}",
            save_history=False,
            framing_suffix="",
            thinking_effort="medium",
            task_complexity="deep_reasoning",
            email_for_triage=emails,
            # Any HTML page the agent generates in this run is the email review →
            # file it under email_review/ (gated, 5-day TTL).
            storage_class="email_review",
            # 5xx on failure → Cloud Tasks retries the task; suppress in-process
            # agent retry so the two retry layers don't multiply.
            suppress_transient_retry=True,
            origin_detail="daily_email_review",
        )

        if is_empty_answer(result):
            # Final: the per-date mark (set at fan-out) stays, so the review does not re-run today.
            await self._empty_answer_final("daily_email_review", user_id)
            return {"status": "empty_answer_final", "emails": len(emails)}, 200

        if not result.delivered:
            # Returning 5xx allows Cloud Tasks queue-level retry to handle
            # transient failures (channel unavailable, agent timeout, etc.).
            logger.warning(
                f"[Worker] daily_email_review: delivery FAILED for {user_id[:8]} "
                f"(emails={len(emails)}, agent_status={result.agent_status}, "
                f"error={result.error})"
            )
            return {
                "error": result.error or "delivery_failed",
                "agent_status": result.agent_status.value,
                "emails": len(emails),
            }, 500

        logger.info(
            f"[Worker] daily_email_review: delivered {len(emails)} emails to Smart for {user_id[:8]}"
        )
        return {"status": "ok", "emails": len(emails)}, 200

    # ------------------------------------------------------------------
    # Billing daily summary
    # ------------------------------------------------------------------

    async def _handle_billing_daily_summary(self) -> Tuple[dict, int]:
        """
        Send daily billing summary to a Slack channel via incoming webhook.
        Aggregates all accounts with activity today into one message.
        Called by Cloud Scheduler at 09:00 Europe/Madrid.
        Requires billing_webhook (SlackWebhookAdapter) to be wired at startup.
        """
        if not self._billing_webhook:
            logger.warning("[Worker] billing_daily_summary: billing_webhook not configured")
            return {"error": "billing_webhook not configured"}, 501

        if not self._account_repo:
            logger.warning("[Worker] billing_daily_summary: account_repo not configured")
            return {"error": "account_repo not configured"}, 501

        accounts = await self._account_repo.list_all_accounts()
        from datetime import timedelta
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
        yesterday_str = yesterday.strftime("%Y-%m-%d")

        lines = [f"📊 *Billing Summary — {yesterday_str}*\n"]
        reported = 0

        for account in accounts:
            u = account.usage
            # Resolve "yesterday" against the calendar, not the raw snapshot: an idle
            # yesterday must report 0, never the last active day's leftover total.
            prev_tokens, prev_cost = u.usage_for_date(yesterday)
            if prev_tokens == 0 and prev_cost == 0.0:
                continue

            owner_id = next(
                (uid for uid, role in account.iam_policy.items() if role == "owner"),
                None,
            )
            profile = await self._user_repo.get_user(owner_id) if owner_id else None
            name = profile.display_name if profile else account.account_id[:8]

            lines.append(
                f"*{name}*\n"
                f"  Yesterday: {prev_tokens:,} tok / ${prev_cost:.4f}\n"
                f"  Month:     {u.monthly_tokens:,} tok / ${u.monthly_cost:.4f}\n"
                f"  Total:     {u.total_tokens:,} tok / ${u.total_cost:.4f}"
            )
            reported += 1

        if reported == 0:
            logger.info("[Worker] billing_daily_summary: no accounts with activity yesterday")
            return {"reported": 0}, 200

        await self._billing_webhook.post("\n\n".join(lines))
        logger.info(f"[Worker] billing_daily_summary: reported {reported} accounts")
        return {"reported": reported}, 200

    # ------------------------------------------------------------------
    # Fact lifecycle sweep (the janitor)
    # ------------------------------------------------------------------

    async def _handle_fact_lifecycle_sweep(self) -> Tuple[dict, int]:
        """Run the fact janitor once. Called by Cloud Scheduler daily at 09:30 Europe/Madrid.

        A failed run is logged with its type and answered 500 (a Cloud Tasks claim is released). The Scheduler job sets no retry,
        on purpose: a retry could overlap a running sweep. The service's 🚨 reports the failure; the next day's run catches up.
        """
        if self._fact_lifecycle_sweep is None:
            logger.warning("[Worker] fact_lifecycle_sweep: sweep service not configured")
            return {"error": "fact lifecycle sweep not configured"}, 501
        try:
            totals = await self._fact_lifecycle_sweep.sweep()
        except Exception as exc:
            logger.error(f"[Worker] fact_lifecycle_sweep failed ({type(exc).__name__})", exc_info=True)
            return {"error": "sweep_failed", "error_type": type(exc).__name__}, 500
        return totals, 200

    async def _handle_fact_review(self, payload: dict) -> Tuple[dict, int]:
        """One memory-review run (`daily`, the default) or the send of the next question (`next`).

        The ids and the mode are checked here, before the service is called: a missing id or a mode that is neither is
        a 400 and nothing runs. Every result of the run is 200-shaped (`completed`, `deferred`, `failed`, `not_owner`);
        a lease, store or queue error raises, so the claim is released and Cloud Tasks retries.
        """
        if self._fact_review is None:
            logger.warning(f"[Worker] {FACT_REVIEW_TASK}: review service not configured")
            return {"error": "fact review not configured"}, 501
        user_id = payload.get("user_id")
        account_id = payload.get("account_id")
        mode = payload.get("mode")
        if not user_id or not account_id:
            logger.warning(f"[Worker] {FACT_REVIEW_TASK}: missing user_id or account_id")
            return {"error": "missing user_id or account_id"}, 400
        if mode is None:  # the service's default mode, `daily`
            return await self._fact_review.run(user_id=user_id, account_id=account_id), 200
        if not isinstance(mode, str) or mode not in REVIEW_MODES:
            logger.warning(f"[Worker] {FACT_REVIEW_TASK}: unknown mode {mode!r}")
            return {"error": "unknown mode"}, 400
        return await self._fact_review.run(user_id=user_id, account_id=account_id, mode=mode), 200

    async def _handle_interactive_choice(self, payload: dict) -> Tuple[dict, int]:
        """One owner's pick on an interactive message (the Slack interactivity route queued it).

        The body is the choice (`InteractiveChoice.to_payload()`); a body that is not one is a 400 and nothing runs. Every
        result of the service is 200-shaped (`shown`, `ignored`, `unknown_user`, `unknown_kind`); a store or platform
        error raises, so the claim is released and Cloud Tasks retries.
        """
        if self._interactive_choice is None:
            logger.warning(f"[Worker] {INTERACTIVE_CHOICE_TASK}: choice service not configured")
            return {"error": "interactive choice not configured"}, 501
        choice = InteractiveChoice.from_payload(payload)
        if choice is None:
            logger.warning(f"[Worker] {INTERACTIVE_CHOICE_TASK}: the payload is not a choice")
            return {"error": "invalid choice"}, 400
        return await self._interactive_choice.handle(choice), 200

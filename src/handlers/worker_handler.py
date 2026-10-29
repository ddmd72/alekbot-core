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
  - execute_reminder         → run one claimed reminder fire: load note, idempotency
                                check via `last_delivered_due`, deliver via notify(),
                                stamp delivery on success
  - setup_microsoft_todo          → TaskSetupService.setup(user_id)
  - reindex_task_list             → TaskIndexingService.reindex_list(user_id, list_id)
  - renew_task_subscriptions      → TaskSetupService.renew_expiring_subscriptions(user_id)
  - renew_all_task_subscriptions  → fan-out: enqueue renew_task_subscriptions for all MS To Do users
  - start_email_indexing          → fan-out: start incremental indexing for all Gmail users with auto_index enabled
  - start_daily_email_review      → fan-out: enqueue daily_email_review for all Gmail users with gmail_daily_review enabled
  - daily_email_review            → fetch last 24h emails, deliver structured payload to SmartAgent for analysis
  - repair_email_embeddings       → run one batch of EmailEmbeddingRepairService (re-embed indexed emails
                                     where embedding_pending=True after transient API failures)
  - deep_research_failed          → the research Cloud Run Job failed: tell the user in the origin channel
  - video_generation_polling      → poll VideoGenerationPort job, deliver via deliver_video()
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

import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional, Tuple


if TYPE_CHECKING:
    from ..services.email_review_service import EmailReviewService
    from ..services.task_indexing_service import TaskIndexingService
    from ..services.task_setup_service import TaskSetupService
    from ..ports.agent_note_port import AgentNotePort
    from ..ports.indexed_email_repository import IndexedEmailRepository
    from ..ports.media_storage_port import MediaStoragePort
    from ..ports.account_repository import AccountRepository
    from ..ports.dedup_store import DedupStore
    from ..ports.task_claim_store import TaskClaimStore
    from ..ports.prompt_content_store import PromptContentStore
    from ..ports.oauth_credentials_port import OAuthCredentialsPort
    from ..ports.video_generation_port import VideoPollResult
    from ..services.file_link_service import FileLinkService
    from ..services.short_link_service import ShortLinkService
    from ..services.companion_extraction_service import CompanionExtractionService
    from ..services.drive_sync_scheduler_service import DriveSyncSchedulerService
    from ..services.fact_lifecycle_sweep_service import FactLifecycleSweepService
    from ..services.fact_review_service import FactReviewService
    from ..services.interactive_choice_service import InteractiveChoiceService
    from ..services.integration_reconnect_service import IntegrationReconnectService

from ..domain.cloud_task_delivery import CloudTaskDelivery
from ..domain.complexity_settings import resolve_complexity_settings
from ..domain import deep_research_notices as dr_notices
from ..domain.exceptions import OAuthGrantRevokedError
from ..domain.fact_review_task_keys import FACT_REVIEW_TASK, REVIEW_MODES
from ..domain.interactive_choice import INTERACTIVE_CHOICE_TASK, InteractiveChoice
from ..domain.notification_kind import NotificationKind
from ..domain.notify_result import empty_answer_alert, is_empty_answer
from ..domain.workspace import fire_key as workspace_fire_key
from ..infrastructure.notification_sla import dispatch_deadline_s
from ..handlers.agent_worker_handler import AgentWorkerHandler
from ..services.deep_research_delivery import NotificationPort, deliver_deep_research
from ..services.consolidation_service import ConsolidationService
from ..services.provider_registry import ProviderRegistry
from ..services.email_embedding_repair_service import EmailEmbeddingRepairService
from ..services.email_indexing_service import EmailIndexingService
from ..services.reminders_service import (
    RemindersService,
    build_reminder_alert,
    build_reminder_alert_summary,
)
from ..services.task_dispatch_service import TaskDispatchService
from ..services.video_generation_delivery import deliver_video
from ..utils.logger import logger


DEFAULT_VIDEO_DURATION_S_FALLBACK = 5  # only used if a poll payload is somehow missing duration_s


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
        coordinator: Any,  # AgentCoordinator (avoid infrastructure import in handlers)
        agent_factory: Any,  # UserAgentFactory
        indexed_email_repo: Optional[IndexedEmailRepository],
        user_repo: Any,  # UserRepository
        task_dispatch: Optional[TaskDispatchService] = None,
        video_registry: Optional[ProviderRegistry] = None,
        quota_service: Optional[object] = None,
        media_storage: Optional[MediaStoragePort] = None,
        task_setup: "Optional[TaskSetupService]" = None,
        task_indexing: "Optional[TaskIndexingService]" = None,
        reminders_service: Optional[RemindersService] = None,
        notes_port: "Optional[AgentNotePort]" = None,
        email_review: "Optional[EmailReviewService]" = None,
        account_repo: "Optional[AccountRepository]" = None,
        billing_webhook: Any = None,  # SlackWebhookAdapter
        email_embedding_repair: Optional[EmailEmbeddingRepairService] = None,
        link_service: "Optional[FileLinkService]" = None,
        short_link_service: "Optional[ShortLinkService]" = None,
        companion_extraction: "Optional[CompanionExtractionService]" = None,
        oauth_credentials: "Optional[OAuthCredentialsPort]" = None,
        drive_sync_scheduler: "Optional[DriveSyncSchedulerService]" = None,
        task_claims: "Optional[TaskClaimStore]" = None,
        daily_review_marks: "Optional[DedupStore]" = None,
        deep_research_fetch: "Optional[Callable[[str], Awaitable[dict]]]" = None,
        prompt_content_store: "Optional[PromptContentStore]" = None,
        fact_lifecycle_sweep: "Optional[FactLifecycleSweepService]" = None,
        fact_review: "Optional[FactReviewService]" = None,
        interactive_choice: "Optional[InteractiveChoiceService]" = None,
        reconnect: "Optional[IntegrationReconnectService]" = None,
    ) -> None:
        self._reconnect = reconnect
        self._fact_lifecycle_sweep = fact_lifecycle_sweep
        self._fact_review = fact_review
        self._interactive_choice = interactive_choice
        self._task_claims = task_claims
        # Keys of attempts running in this process. Check-and-add has no await between, so
        # asyncio's single thread makes it race-free without a lock.
        self._running_claims: set[str] = set()
        self._deep_research_fetch = deep_research_fetch
        self._prompt_content_store = prompt_content_store
        self._daily_review_marks = daily_review_marks
        self._slack_worker: Optional[Callable[[dict, dict], Awaitable[Tuple[dict, int]]]] = None
        self._telegram_worker: Optional[Callable[[dict], Awaitable[Tuple[dict, int]]]] = None
        self._agent_worker = agent_worker_handler
        self._email_indexing = email_indexing_service
        self._notification = notification_service
        self._consolidation = consolidation_service
        self._coordinator = coordinator
        self._agent_factory = agent_factory
        self._indexed_email_repo = indexed_email_repo
        self._user_repo = user_repo
        self._task_dispatch = task_dispatch
        self._video_registry: Optional[ProviderRegistry] = video_registry
        self._quota_service = quota_service
        self._media_storage = media_storage
        self._link_service = link_service
        self._short_link_service = short_link_service
        self._task_setup = task_setup
        self._task_indexing = task_indexing
        self._reminders_service = reminders_service
        self._notes_port = notes_port
        self._email_review = email_review
        self._account_repo = account_repo
        self._billing_webhook = billing_webhook
        self._email_embedding_repair = email_embedding_repair
        self._companion_extraction = companion_extraction
        self._oauth_credentials = oauth_credentials
        self._drive_sync_scheduler = drive_sync_scheduler

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
        if task_type == "agent_execution":
            user_id = payload.get("context", {}).get("user_id", "")
            if user_id:
                await self._agent_factory.ensure_agents_for_user(user_id)
            result = await self._agent_worker.handle_task(payload)
            return result, 200
        elif task_type == "telegram_update":
            if self._telegram_worker is None:
                logger.error("[WorkerHandler] telegram_update delivered but Telegram is not configured")
                return {"error": "telegram not configured"}, 503
            return await self._telegram_worker(payload)
        elif task_type == "email_indexing":
            return await self._handle_email_indexing(payload)
        elif task_type == "email_indexing_watchdog":
            return await self._handle_watchdog()
        elif task_type == "consolidation":
            return await self._handle_consolidation(payload)
        elif task_type == "sweep_consolidation":
            return await self._handle_sweep_consolidation()
        elif task_type == "companion_consolidation":
            return await self._handle_companion_consolidation(payload)
        elif task_type == "sweep_companion_consolidation":
            return await self._handle_sweep_companion_consolidation()
        elif task_type == "deep_research_failed":
            return await self._handle_deep_research_failed(payload)
        elif task_type == "deep_research_webhook_event":
            return await self._handle_deep_research_webhook_event(payload)
        elif task_type == "video_generation_polling":
            return await self._handle_video_generation_polling(payload)
        elif task_type == "setup_microsoft_todo":
            return await self._handle_setup_microsoft_todo(payload)
        elif task_type == "reindex_task_list":
            return await self._handle_reindex_task_list(payload)
        elif task_type == "renew_task_subscriptions":
            return await self._handle_renew_task_subscriptions(payload)
        elif task_type == "renew_all_task_subscriptions":
            return await self._handle_renew_all_task_subscriptions()
        elif task_type == "fire_due_reminders":
            return await self._handle_fire_due_reminders()
        elif task_type == "execute_reminder":
            return await self._handle_execute_reminder(payload)
        elif task_type == "start_email_indexing":
            return await self._handle_start_email_indexing()
        elif task_type == "start_daily_email_review":
            return await self._handle_start_daily_email_review()
        elif task_type == "daily_email_review":
            return await self._handle_daily_email_review(payload)
        elif task_type == "billing_daily_summary":
            return await self._handle_billing_daily_summary()
        elif task_type == "repair_email_embeddings":
            return await self._handle_repair_email_embeddings()
        elif task_type == "start_drive_sync":
            return await self._handle_start_drive_sync()
        elif task_type == "reconnect_notice_sweep":
            return await self._handle_reconnect_notice_sweep()
        elif task_type == "fact_lifecycle_sweep":
            return await self._handle_fact_lifecycle_sweep()
        elif task_type == FACT_REVIEW_TASK:
            return await self._handle_fact_review(payload)
        elif task_type == INTERACTIVE_CHOICE_TASK:
            return await self._handle_interactive_choice(payload)
        elif task_type is None and "event" in payload and "session_id" in payload:
            # Queued Slack event (GcpTaskQueue.enqueue_slack_event): no task_type by design.
            if self._slack_worker is None:
                logger.error("[WorkerHandler] Slack event delivered but no Slack worker is attached")
                return {"error": "slack not configured"}, 503
            return await self._slack_worker(payload, headers)
        logger.error("[WorkerHandler] unknown task_type=%r", task_type)
        return {"error": "unknown task_type"}, 400

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
    # Email embedding repair
    # ------------------------------------------------------------------

    async def _handle_repair_email_embeddings(self) -> Tuple[dict, int]:
        """Run one repair batch for indexed emails with `embedding_pending=True`.

        Triggered hourly by Cloud Scheduler. Cross-user, batch-capped at
        EmailEmbeddingRepairService.batch_size (default 100). When the batch
        saturates, immediately re-enqueues another `repair_email_embeddings`
        task so the queue drains without waiting for the next scheduler tick
        — same pattern as `_handle_consolidation` and `_handle_email_indexing`.
        """
        if self._email_embedding_repair is None:
            logger.warning("[Worker] repair_email_embeddings: service not configured")
            return {"error": "repair service not configured"}, 501

        repaired, has_more = await self._email_embedding_repair.run()
        if has_more and self._task_dispatch:
            await self._task_dispatch.enqueue_worker_task(
                task_type="repair_email_embeddings",
                payload={},
            )
            logger.info("📬 [Worker] Re-enqueued next email-embedding repair tick")
        return {"status": "ok", "repaired": repaired, "has_more": has_more}, 200

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

    # ------------------------------------------------------------------
    # Deep research job failure notice
    # ------------------------------------------------------------------

    async def _handle_deep_research_failed(self, payload: dict) -> Tuple[dict, int]:
        """The research job (Cloud Run Job) cannot notify; it enqueues this task to tell the user."""
        user_id = payload.get("user_id")
        account_id = payload.get("account_id")
        if not user_id or not account_id:
            return {"error": "missing user_id or account_id"}, 400
        session_id = payload.get("session_id") or ""
        origin_channel_id = session_id.split(":", 1)[1] if ":" in session_id else None
        query = payload.get("query") or ""
        # notify() routes through the eager Quick agent, absent on a cold instance until this runs.
        await self._agent_factory.ensure_agents_for_user(user_id)
        alert = (
            dr_notices.report_not_prepared_notice(query)
            if payload.get("reason") == "delivery_failed"
            else dr_notices.research_failed_notice(query)
        )
        return await self._send_deep_research_notice(
            user_id, account_id, alert, origin_channel_id, payload.get("origin_platform") or None,
            context=f"reason={payload.get('reason')}",
        )

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

    async def _send_deep_research_notice(
        self, user_id: str, account_id: str, alert: str,
        origin_channel_id: Optional[str], origin_platform: Optional[str], context: str,
    ) -> Tuple[dict, int]:
        """One deep-research notice; 5xx (Cloud Tasks retries, claim released) only when nothing was sent."""
        result = await self._notification.notify(
            kind=NotificationKind.DEEP_RESEARCH,
            user_id=user_id,
            account_id=account_id,
            system_alert=alert,
            channel_id_override=origin_channel_id,
            platform_override=origin_platform,
            # This handler's 5xx makes Cloud Tasks retry; the agent must not retry inside as well.
            suppress_transient_retry=True,
        )
        if is_empty_answer(result):
            await self._empty_answer_final("deep_research_notice", user_id)
            return {"status": "empty_answer_final"}, 200
        if not result.delivered and not result.sent:
            logger.warning(
                "[DeepResearch] notice not sent: user=%s %s error=%s", user_id[:8], context, result.error,
            )
            return {"error": result.error or "delivery_failed"}, 500
        if not result.delivered:
            # A resend would double a message that may already be on the channel.
            logger.warning(
                "[DeepResearch] notice sent but not confirmed: user=%s %s error=%s; not retried",
                user_id[:8], context, result.error,
            )
        return {"status": "ok"}, 200

    # ------------------------------------------------------------------
    # OpenAI deep research webhook event (the webhook only enqueues this)
    # ------------------------------------------------------------------

    async def _handle_deep_research_webhook_event(self, payload: dict) -> Tuple[dict, int]:
        """Deliver one OpenAI deep-research outcome: report for a completed job, a notice otherwise.
        The report text is re-read by job id here, so it never travels through Cloud Tasks."""
        event_type = payload.get("event_type") or ""
        job_id = payload.get("job_id") or ""
        user_id = payload.get("user_id") or ""
        account_id = payload.get("account_id") or ""
        if not (event_type and job_id and user_id and account_id):
            return {"error": "missing event_type, job_id, user_id or account_id"}, 400
        if self._deep_research_fetch is None:
            # 4xx: Cloud Tasks would retry a 5xx forever against a service that can never retrieve.
            logger.error("[DeepResearch] webhook event delivered but OpenAI retrieval is not configured")
            return {"error": "retrieval not configured"}, 422
        session_id = payload.get("session_id") or ""
        origin_channel_id = session_id.split(":", 1)[1] if ":" in session_id else None
        origin_platform = payload.get("origin_platform") or None
        query = payload.get("query") or ""

        try:
            response = await self._deep_research_fetch(job_id)
        except Exception as exc:
            logger.error("[DeepResearch] response retrieval failed: job=%s error=%s", job_id[:16], exc, exc_info=True)
            return {"error": "retrieval_failed"}, 500
        await self._agent_factory.ensure_agents_for_user(user_id)

        if event_type == "response.completed":
            output_text = response.get("output_text") or ""
            # Durable capture BEFORE delivery: research is expensive and the result must survive a failed delivery.
            if self._prompt_content_store is not None:
                await self._prompt_content_store.record_dr_result(
                    output_text=output_text, query=query, user_id=user_id, account_id=account_id,
                    model=response.get("model", ""), provider="openai", source="openai_webhook", job_id=job_id,
                )
            handed_off = await deliver_deep_research(
                result_text=output_text,
                user_id=user_id,
                account_id=account_id,
                query=query,
                task_queue=self._task_dispatch,
                session_id=session_id,
                media_storage=self._media_storage,
                link_service=self._link_service,
                channel_id_override=origin_channel_id,
                platform_override=origin_platform,
                failure_notification=self._notification,
                report_dedup_id=job_id,
            )
            if not handed_off:
                # deliver_deep_research has notified the user (or logged that the notice failed); a retry would upload and notify again.
                logger.error("[DeepResearch] report task not enqueued: job=%s user=%s", job_id[:16], user_id[:8])
                return {"status": "report_not_enqueued"}, 200
            logger.info("[DeepResearch] report delivered: user=%s job=%s", user_id[:8], job_id[:16])
            return {"status": "ok"}, 200

        if event_type == "response.failed":
            error = (response.get("error") or {}).get("message", "unknown error")
            logger.error("[DeepResearch] job failed: job=%s error=%s", job_id[:16], error)
            alert = dr_notices.provider_error_notice(query)
        elif event_type == "response.incomplete":
            reason = (response.get("incomplete_details") or {}).get("reason", "unknown")
            logger.warning("[DeepResearch] job incomplete: job=%s reason=%s", job_id[:16], reason)
            alert = dr_notices.incomplete_notice(query)
        elif event_type == "response.cancelled":
            logger.info("[DeepResearch] job cancelled: job=%s", job_id[:16])
            alert = dr_notices.cancelled_notice(query)
        else:
            logger.error("[DeepResearch] unknown webhook event type %r: job=%s", event_type, job_id[:16])
            return {"error": "unknown event_type"}, 400
        return await self._send_deep_research_notice(
            user_id, account_id, alert, origin_channel_id, origin_platform, context=f"event={event_type}",
        )

    # ------------------------------------------------------------------
    # Video generation polling (Grok)
    # ------------------------------------------------------------------

    _MAX_VIDEO_POLL_ATTEMPTS = 24  # ~24 x 30s = 12 min budget, RFC §10 open question #3 starting point

    async def _handle_video_generation_polling(self, payload: dict) -> Tuple[dict, int]:
        """
        Poll video generation status. Re-enqueue if pending. On done: deliver via
        deliver_video(). On failed/expired: notify the user directly, no silent
        drop. Kicked off by GrokVideoAdapter. See VIDEO_GENERATION_RFC.md §3.4 step 4.

        duration_s is resolved ONCE here (not re-read in the pending/done branches)
        so it survives multiple poll cycles: the re-enqueue call below threads it
        forward on every "pending" tick, otherwise TaskDispatchService's own
        duration_s=5 default would silently reset it on each re-poll and the
        deliver_video() billing call would under-bill a longer video.

        origin_platform is resolved the same way, for the same reason, but for a
        different bug: UserNotificationService.resolve_channel() only honors
        channel_id_override when platform_override is ALSO set (`if
        channel_id_override and platform_override:`) — omitting platform_override
        silently falls through to the user's primary/last-active channel instead
        of the channel the request actually came from. Live-verified 2026-08-24.
        """
        request_id      = payload.get("request_id", "")
        user_id         = payload.get("user_id", "")
        account_id      = payload.get("account_id", "")
        session_id      = payload.get("session_id", "")
        attempt         = payload.get("attempt", 0)
        provider        = payload.get("provider", "grok")
        duration_s      = payload.get("duration_s", DEFAULT_VIDEO_DURATION_S_FALLBACK)
        origin_platform = payload.get("origin_platform")

        origin_channel_id = session_id.split(":", 1)[1] if ":" in session_id else None

        video_port = None
        if self._video_registry:
            try:
                video_port = self._video_registry.get(provider)
            except ValueError:
                logger.debug("No video generation port registered for provider %r", provider)
        if not video_port or not self._task_dispatch:
            logger.error(
                f"[VideoGeneration] Missing dependencies in WorkerHandler "
                f"(provider={provider!r}, available="
                f"{self._video_registry.list_available() if self._video_registry else []})"
            )
            return {"error": "video_generation not configured"}, 500

        # notify() routes through the eager Quick agent, absent on a cold instance until this runs.
        if user_id:
            await self._agent_factory.ensure_agents_for_user(user_id)

        if attempt >= self._MAX_VIDEO_POLL_ATTEMPTS:
            logger.warning(f"[VideoGeneration] Polling timeout: request={request_id[:16]}")
            await self._notification.notify(
                user_id=user_id, account_id=account_id,
                system_alert="Video generation timed out without producing a result.",
                kind=NotificationKind.DEEP_RESEARCH,
                channel_id_override=origin_channel_id,
                platform_override=origin_platform,
            )
            return {"status": "timeout"}, 200

        try:
            result: VideoPollResult = await video_port.get_status(request_id)
        except Exception as exc:
            logger.warning(
                f"[VideoGeneration] get_status failed, attempt={attempt}: {exc}"
            )
            await self._task_dispatch.enqueue_video_generation_polling(
                request_id=request_id, user_id=user_id, account_id=account_id,
                session_id=session_id, attempt=attempt + 1, delay_seconds=30,
                duration_s=duration_s, origin_platform=origin_platform,
            )
            return {"status": "retry", "attempt": attempt + 1}, 200

        if result.status == "pending":
            await self._task_dispatch.enqueue_video_generation_polling(
                request_id=request_id, user_id=user_id, account_id=account_id,
                session_id=session_id, attempt=attempt + 1, delay_seconds=30,
                duration_s=duration_s, origin_platform=origin_platform,
            )
            logger.info(f"[VideoGeneration] Pending, attempt={attempt + 1}")
            return {"status": "polling", "attempt": attempt + 1}, 200

        if result.status == "done":
            # Prefer the duration xAI actually reports on the finished render
            # (result.duration_s) over the poll payload's submission-time estimate —
            # the payload's value is a guess for create_video and simply wrong for
            # edit_video (which has no duration param at submission time at all).
            # Fall back to the payload only when the poll response didn't include one.
            effective_duration_s = (
                result.duration_s if result.duration_s is not None else duration_s
            )
            await deliver_video(
                video_data=result.data,
                user_id=user_id, account_id=account_id,
                duration_s=effective_duration_s,
                media_storage=self._media_storage,
                notification=self._notification,
                quota_service=self._quota_service,
                link_service=self._link_service,
                short_link_service=self._short_link_service,
                channel_id_override=origin_channel_id,
                platform_override=origin_platform,
            )
            logger.info(f"[VideoGeneration] Delivered to user={user_id[:8]}")
            return {"status": "delivered"}, 200

        # "failed" or "expired"
        logger.error(
            f"[VideoGeneration] {result.status}: request={request_id[:16]}, error={result.error}"
        )
        # result.error carries xAI's actual rejection reason (e.g. "Video is too
        # long. Maximum duration is 8.7 seconds.") — surface it verbatim instead of
        # a generic apology, so the user (and the formatter agent relaying this)
        # knows exactly what to fix, not just that something failed.
        alert = "Video generation did not complete — the AI provider returned an error."
        if result.error:
            alert += f" Reason: {result.error}"
        await self._notification.notify(
            user_id=user_id, account_id=account_id,
            system_alert=alert,
            kind=NotificationKind.DEEP_RESEARCH,
            channel_id_override=origin_channel_id,
            platform_override=origin_platform,
        )
        return {"status": result.status}, 200

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

    async def _handle_execute_reminder(self, payload: dict) -> Tuple[dict, int]:
        """Run a single claimed reminder fire (Step #8 of NOTIFICATION_DELIVERY_REFACTOR_RFC).

        Payload (placed by ``RemindersService.fire_due_reminders``):
            note_id : str          Firestore note id
            user_id : str          owning user (verified via ownership-scoped get_note)
            due_at  : str (ISO)    fire-time this task is delivering — used as
                                   the idempotency token (compared against
                                   ``last_delivered_due``).

        Outcomes (HTTP status drives Cloud Tasks retry policy — 5xx = retry,
        2xx = ack and stop):
            note_gone           note deleted between cron-tick and this run → 200
            no_user             user repo lost the user → 200
            already_delivered   ``last_delivered_due == due_at`` → 200 (Cloud
                                Tasks retry no-op, prevents duplicate Smart run
                                + duplicate user message)
            delivery_failed     notify() returned ``delivered=False`` → 500
                                (Cloud Tasks queue retries with backoff)
            ok                  delivered + ``mark_fire_delivered`` stamped → 200
        """
        if not self._notes_port:
            logger.warning("[Worker] execute_reminder: notes_port not configured")
            return {"error": "notes_port not configured"}, 501

        note_id = payload.get("note_id")
        user_id = payload.get("user_id")
        due_at_raw = payload.get("due_at")
        if not note_id or not user_id or not due_at_raw:
            return {"error": "missing note_id, user_id, or due_at"}, 400

        try:
            due_at = datetime.fromisoformat(due_at_raw)
        except ValueError:
            logger.warning(
                "[Worker] execute_reminder: bad due_at %r for note=%s",
                due_at_raw, note_id,
            )
            return {"error": "bad due_at"}, 400

        note = await self._notes_port.get_note(user_id=user_id, note_id=note_id)
        if note is None:
            # One-time already delivered-and-deleted by a prior worker run, or
            # a note the user has since removed. Either way: nothing to
            # deliver, ack the task.
            logger.info(
                "[Worker] execute_reminder: note_gone note=%s user=%s",
                note_id, user_id[:8],
            )
            return {"status": "note_gone"}, 200

        # Idempotency guard against Cloud Tasks retries.
        if note.last_delivered_due is not None and note.last_delivered_due == due_at:
            # Clean up a one-time note that was stamped but not yet deleted
            # (prior run crashed between mark_fire_delivered and delete_note).
            if note.recurrence is None:
                await self._notes_port.delete_note(note_id=note_id, user_id=user_id)
            logger.info(
                "[Worker] execute_reminder: already_delivered note=%s due=%s",
                note_id, due_at.isoformat(),
            )
            return {"status": "already_delivered"}, 200

        user_profile = await self._user_repo.get_user(user_id)
        if not user_profile or not getattr(user_profile, "account_id", None):
            logger.warning(
                "[Worker] execute_reminder: no_user user=%s note=%s",
                user_id[:8], note_id,
            )
            return {"status": "no_user"}, 200

        await self._agent_factory.ensure_agents_for_user(user_id)

        task_complexity = note.complexity.value if note.complexity else "simple_analytics"
        # Resolve the PerformanceTier for the SLA budget through the SAME merge
        # TaskExecutionResolver applies to pick the execution context — user
        # ``complexity_settings_overrides`` on top of the system defaults. Reading the
        # raw defaults table here (as this did until 2026-08-15) set the clock for one
        # tier while the work ran at another: a user overriding simple_analytics to
        # PERFORMANCE got 600s of budget for a 1500s workload, and the daily briefing
        # was killed mid-turn three runs in a row.
        # ``tier=None`` means "use the kind's default budget" (handled by notify());
        # only the kinds with ``tier_overrides`` actually consume this argument.
        sla_tier = None
        if note.complexity is not None:
            settings = resolve_complexity_settings(note.complexity, user_profile.config)
            if settings is not None:
                sla_tier = settings.tier

        fire = workspace_fire_key(note_id, due_at)
        result = await self._notification.notify(
            user_id=user_id,
            account_id=user_profile.account_id,
            system_alert=build_reminder_alert(note),
            system_alert_summary=build_reminder_alert_summary(note),
            kind=NotificationKind.REMINDER,
            tier=sla_tier,
            agent_id_override=f"smart_response_agent_{user_id}",
            task_complexity=task_complexity,
            # This handler returns 5xx on failure → Cloud Tasks retries the task.
            # Suppress the agent's in-process retry so the two layers don't multiply.
            suppress_transient_retry=True,
            # Who started the run, for the workspace's origin labels; the same due_at gives the same key on every
            # Cloud Tasks re-run of this fire (AGENT_WORKSPACE_RFC §9, §10 row 1).
            origin_detail="execute_reminder",
            fire_key=fire,
        )

        if is_empty_answer(result):
            # Final (owner decision 2026-10-23): a retry would repeat the SYNC actions the turn took, and a
            # conditional reminder may end empty every time. Take the success path below.
            await self._empty_answer_final("execute_reminder", user_id, note_id)
        elif not result.delivered:
            logger.warning(
                "[Worker] execute_reminder: delivery_failed note=%s user=%s "
                "(agent_status=%s, error=%s)",
                note_id, user_id[:8], result.agent_status, result.error,
            )
            return {
                "error": result.error or "delivery_failed",
                "agent_status": result.agent_status.value,
            }, 500

        # Stamp idempotency FIRST, then remove a fired one-time note. Order
        # matters: if we crash between the two, a Cloud Tasks retry sees
        # last_delivered_due == due_at and short-circuits (already_delivered)
        # rather than re-delivering.
        await self._notes_port.mark_fire_delivered(note_id=note_id, due_at=due_at)
        if note.recurrence is None:
            # One-time reminder: claimed (not deleted) at fire-time so its
            # content survived for this delivery; remove it now that it fired.
            await self._notes_port.delete_note(note_id=note_id, user_id=user_id)
        logger.info(
            "[Worker] execute_reminder: %s note=%s user=%s due=%s",
            "empty_answer_final" if is_empty_answer(result) else "ok", note_id, user_id[:8], due_at.isoformat(),
        )
        return {"status": "empty_answer_final" if is_empty_answer(result) else "ok"}, 200

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
        if not self._oauth_credentials:
            logger.warning("[Worker] Gmail grant revoked but oauth_credentials not configured")
            return
        try:
            creds = await self._oauth_credentials.get_credentials(user_id, "gmail")
            if creds is None or creds.needs_reconnect:
                return
            # Before the save: a failure here must leave the flag down so the next run retries the notice.
            await self._agent_factory.ensure_agents_for_user(user_id)
            creds.needs_reconnect = True
            if not await self._oauth_credentials.update_credentials(creds):
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

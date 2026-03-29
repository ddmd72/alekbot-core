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
  - deep_research_polling    → poll Gemini job, deliver via notification service
  - fire_due_reminders       → fire all reminders with due <= now, reschedule or delete
  - setup_microsoft_todo          → TaskSetupService.setup(user_id)
  - reindex_task_list             → TaskIndexingService.reindex_list(user_id, list_id)
  - renew_task_subscriptions      → TaskSetupService.renew_expiring_subscriptions(user_id)
  - renew_all_task_subscriptions  → fan-out: enqueue renew_task_subscriptions for all MS To Do users
  - start_email_indexing          → fan-out: start incremental indexing for all Gmail users with auto_index enabled
  - start_daily_email_review      → fan-out: enqueue daily_email_review for all Gmail users with gmail_daily_review enabled
  - daily_email_review            → fetch last 24h emails, deliver structured payload to SmartAgent for analysis
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, List, Optional, Tuple
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta

if TYPE_CHECKING:
    from ..services.task_indexing_service import TaskIndexingService
    from ..services.task_setup_service import TaskSetupService

from ..domain.agent_note import ReminderRecurrence
from ..ports.agent_note_port import AgentNotePort

from ..domain.agent import AgentIntent, AgentMessage, AgentStatus
from ..handlers.agent_worker_handler import AgentWorkerHandler
from ..handlers.consolidation_handler import process_user_batches_on_overflow
from ..services.deep_research_delivery import (
    NotificationPort, deliver_deep_research,
)
from ..ports.consolidation_queue import ConsolidationQueue
from ..ports.media_storage_port import MediaStoragePort
from ..services.provider_registry import ProviderRegistry
from ..ports.email_indexing_job_repository import EmailIndexingJobRepository
from ..ports.email_provider_port import EmailProviderPort
from ..ports.indexed_email_repository import IndexedEmailRepository
from ..ports.oauth_credentials_port import OAuthCredentialsPort
from ..ports.task_queue import TaskQueue
from ..services.email_indexing_service import EmailIndexingService
from ..utils.logger import logger
from ..utils.debug_logger import get_debug_logger


class WorkerHandler:
    """
    Dispatches /worker HTTP payloads to specialized handlers.

    Owns no business logic — delegates to existing handlers/services.
    Falls back to slack_adapter._handle_worker_task() for unknown task_types.
    """

    def __init__(
        self,
        agent_worker_handler: AgentWorkerHandler,
        email_indexing_service: EmailIndexingService,
        email_job_repo: EmailIndexingJobRepository,
        oauth_credentials: OAuthCredentialsPort,
        notification_service: NotificationPort,
        consolidation_queue: ConsolidationQueue,
        coordinator: Any,  # AgentCoordinator (avoid infrastructure import in handlers)
        agent_factory: Any,  # UserAgentFactory
        indexed_email_repo: Optional[IndexedEmailRepository],
        user_repo: Any,  # UserRepository
        task_queue: Optional[TaskQueue] = None,
        job_registry: Optional[ProviderRegistry] = None,
        media_storage: Optional[MediaStoragePort] = None,
        task_setup: "Optional[TaskSetupService]" = None,
        task_indexing: "Optional[TaskIndexingService]" = None,
        notes_port: Optional[AgentNotePort] = None,
        email_provider: Optional[EmailProviderPort] = None,
    ) -> None:
        self._agent_worker = agent_worker_handler
        self._email_indexing = email_indexing_service
        self._email_job_repo = email_job_repo
        self._oauth = oauth_credentials
        self._notification = notification_service
        self._consolidation_queue = consolidation_queue
        self._coordinator = coordinator
        self._agent_factory = agent_factory
        self._indexed_email_repo = indexed_email_repo
        self._user_repo = user_repo
        self._task_queue = task_queue
        self._job_registry: Optional[ProviderRegistry] = job_registry
        self._media_storage = media_storage
        self._task_setup = task_setup
        self._task_indexing = task_indexing
        self._notes_port = notes_port
        self._email_provider = email_provider

    async def handle(self, payload: dict) -> Optional[Tuple[dict, int]]:
        """
        Dispatch to appropriate handler by task_type.

        Returns (body_dict, status_code) for known task_types, or None for
        unknown types (caller should handle fallback).
        """
        task_type = payload.get("task_type")
        if task_type == "agent_execution":
            user_id = payload.get("context", {}).get("user_id", "")
            if user_id:
                await self._agent_factory.ensure_agents_for_user(user_id)
            result = await self._agent_worker.handle_task(payload)
            return result, 200
        elif task_type == "email_indexing":
            return await self._handle_email_indexing(payload)
        elif task_type == "email_indexing_watchdog":
            return await self._handle_watchdog()
        elif task_type == "consolidation":
            return await self._handle_consolidation(payload)
        elif task_type == "deep_research_polling":
            return await self._handle_deep_research_polling(payload)
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
        elif task_type == "start_email_indexing":
            return await self._handle_start_email_indexing()
        elif task_type == "start_daily_email_review":
            return await self._handle_start_daily_email_review()
        elif task_type == "daily_email_review":
            return await self._handle_daily_email_review(payload)
        return None  # unknown task_type — caller handles fallback

    # ------------------------------------------------------------------
    # Email indexing
    # ------------------------------------------------------------------

    async def _handle_start_email_indexing(self) -> Tuple[dict, int]:
        """
        Fan-out: start incremental indexing for all Gmail users with auto_index enabled
        and whose auto_index_hour matches the current hour in their timezone.
        Called by Cloud Scheduler hourly.
        """
        if not self._oauth or not self._email_job_repo or not self._task_queue:
            logger.warning("[Worker] start_email_indexing: required services not configured")
            return {"error": "services not configured"}, 501

        user_ids = await self._oauth.list_users_by_provider("gmail")
        now_utc = datetime.now(timezone.utc)
        started, skipped = 0, 0

        for user_id in user_ids:
            profile = await self._user_repo.get_user(user_id)
            if not profile:
                skipped += 1
                continue

            cfg = profile.config
            if not cfg.gmail_auto_index:
                skipped += 1
                continue

            user_tz = ZoneInfo(cfg.timezone or "UTC")
            local_hour = now_utc.astimezone(user_tz).hour
            if local_hour != cfg.gmail_auto_index_hour:
                skipped += 1
                continue

            # Check no indexing job already running
            latest_job = await self._email_job_repo.get_latest_job(user_id, "gmail")
            if latest_job and latest_job.status == "running":
                logger.info(f"[Worker] start_email_indexing: job already running for {user_id[:8]}, skipping")
                skipped += 1
                continue

            creds = await self._oauth.get_credentials(user_id, "gmail")
            if not creds:
                logger.warning(f"[Worker] start_email_indexing: no credentials for {user_id[:8]}")
                skipped += 1
                continue

            job = self._email_indexing.create_job(
                user_id=user_id,
                provider="gmail",
                triggered_by="scheduler",
                mode="incremental",
                account_id=profile.account_id,
            )
            await self._email_job_repo.create_job(job)
            await self._task_queue.enqueue_email_indexing_task(job.job_id)
            logger.info(f"[Worker] start_email_indexing: enqueued job {job.job_id[:8]} for {user_id[:8]}")
            started += 1

        logger.info(f"[Worker] start_email_indexing complete: started={started}, skipped={skipped}")
        return {"started": started, "skipped": skipped}, 200

    async def _handle_email_indexing(self, payload: dict) -> Tuple[dict, int]:
        """
        Process one page of email indexing. Re-enqueues if more pages remain.
        Sends user notification on completion.
        """
        job_id = payload.get("job_id")
        logger.info(f"📧 [Worker] email_indexing received: job_id={job_id}")
        if not job_id:
            return {"error": "missing job_id"}, 400

        job = await self._email_job_repo.get_job(job_id)
        if not job:
            return {"error": "job not found"}, 404
        if job.status != "running":
            logger.info(f"📧 [Worker] Job {job_id[:8]} is {job.status}, skipping")
            return {"status": "skipped", "reason": job.status}, 200

        creds = await self._oauth.get_credentials(job.user_id, job.provider)
        if not creds:
            await self._email_job_repo.update_job(
                job_id, {"status": "failed_auth", "updated_at": datetime.utcnow()}
            )
            return {"error": "oauth credentials missing"}, 400

        try:
            job = await self._email_indexing.run_indexing_job(
                job=job,
                credentials=creds,
                account_id=job.account_id,
                max_pages=1,
                mode=job.mode,
                backfill_until=job.backfill_until,
            )
        except Exception as exc:
            # Service already updated job status (failed_auth / failed) and logged the error.
            # Return 200 so Cloud Tasks does not retry — retrying a failed_auth job is pointless.
            logger.warning(f"⚠️ [Worker] Indexing job {job_id[:8]} raised (status updated by service): {exc}")
            return {"status": "failed", "error": str(exc)}, 200

        if job.next_page_token and self._task_queue:
            await self._task_queue.enqueue_email_indexing_task(job.job_id)
            logger.info(f"📬 [Worker] Re-enqueued email indexing page for job {job.job_id[:8]}")
        elif not job.next_page_token:
            logger.info(f"✅ [Worker] Indexing job {job_id[:8]} complete: stored={job.emails_stored}, failed={job.emails_failed}")

        return {"status": "ok", "has_more": bool(job.next_page_token)}, 200

    # ------------------------------------------------------------------
    # Watchdog
    # ------------------------------------------------------------------

    async def _handle_watchdog(self) -> Tuple[dict, int]:
        """Mark stale 'running' jobs as failed. Triggered by Cloud Scheduler."""
        stale_threshold = datetime.utcnow() - timedelta(hours=2)
        stale_jobs = await self._email_job_repo.get_stale_running_jobs(stale_threshold)
        marked = 0
        for stale_job in stale_jobs:
            await self._email_job_repo.update_job(stale_job.job_id, {
                "status": "failed",
                "updated_at": datetime.utcnow(),
            })
            marked += 1
            logger.warning(f"⏰ [Watchdog] Marked stale job {stale_job.job_id[:8]} as failed")
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
        if not user_id or self._agent_factory is None:
            return {"error": "missing user_id or factory not ready"}, 400

        has_more = await process_user_batches_on_overflow(
            user_id=user_id,
            coordinator=self._coordinator,
            agent_factory=self._agent_factory,
            queue=self._consolidation_queue,
            max_batches=1,
            indexed_email_repo=self._indexed_email_repo,
            user_repo=self._user_repo,
        )
        if has_more and self._task_queue:
            await self._task_queue.enqueue_consolidation_task(user_id=user_id)
            logger.info(f"📬 [Worker] Re-enqueued next consolidation task for user {user_id[:8]}")
        return {"status": "ok"}, 200

    # ------------------------------------------------------------------
    # Deep Research polling (Gemini)
    # ------------------------------------------------------------------

    _MAX_POLL_ATTEMPTS = 30   # 30 × 120s = 60 min = Gemini Deep Research max window

    async def _handle_deep_research_polling(self, payload: dict) -> Tuple[dict, int]:
        """
        Poll deep research interaction status. Re-enqueue if in_progress.
        On completion: deliver via notification service.

        Each invocation is its own Cloud Tasks HTTP request → full Cloud Run CPU.
        No sleep between attempts — delay is handled by Cloud Tasks schedule_time.
        """
        interaction_id     = payload.get("interaction_id", "")
        user_id            = payload.get("user_id", "")
        account_id         = payload.get("account_id", "")
        query              = payload.get("query", "")
        attempt            = payload.get("attempt", 0)
        consecutive_errors = payload.get("consecutive_errors", 0)
        provider           = payload.get("provider", "gemini")
        session_id         = payload.get("session_id", "")

        job_port = None
        if self._job_registry:
            try:
                job_port = self._job_registry.get(provider)
            except ValueError:
                pass
        if not job_port or not self._task_queue:
            logger.error(
                f"[DeepResearch] Missing dependencies in WorkerHandler "
                f"(provider={provider!r}, available="
                f"{self._job_registry.list_available() if self._job_registry else []})"
            )
            return {"error": "deep_research not configured"}, 500

        await self._agent_factory.ensure_agents_for_user(user_id)

        if attempt >= self._MAX_POLL_ATTEMPTS:
            logger.warning(f"[DeepResearch] Polling timeout: interaction={interaction_id[:16]}")
            await self._notification.notify(
                user_id=user_id,
                account_id=account_id,
                system_alert="Deep research timed out after 60 minutes without producing a result.",
            )
            return {"status": "timeout"}, 200

        try:
            status, payload_text = await job_port.get_status(interaction_id)
            consecutive_errors = 0  # reset on any successful API response
        except Exception as exc:
            consecutive_errors += 1
            logger.warning(
                f"[DeepResearch] Poll attempt={attempt} failed "
                f"(consecutive_errors={consecutive_errors}): {exc}"
            )
            # 5 consecutive errors → interaction is dead, not a transient blip.
            if consecutive_errors >= 5:
                logger.error(
                    f"[DeepResearch] Interaction presumed dead after {consecutive_errors} "
                    f"consecutive errors: interaction={interaction_id[:16]}"
                )
                await self._notification.notify(
                    user_id=user_id,
                    account_id=account_id,
                    system_alert="Deep research did not complete — the research session expired or encountered a persistent error.",
                )
                return {"status": "dead", "attempt": attempt}, 200

            await self._task_queue.enqueue_deep_research_polling(
                interaction_id=interaction_id,
                user_id=user_id,
                account_id=account_id,
                query=query,
                attempt=attempt + 1,
                consecutive_errors=consecutive_errors,
                delay_seconds=120,
                provider=provider,
                session_id=session_id,
            )
            return {"status": "retry", "attempt": attempt + 1}, 200

        if status == "in_progress":
            await self._task_queue.enqueue_deep_research_polling(
                interaction_id=interaction_id,
                user_id=user_id,
                account_id=account_id,
                query=query,
                attempt=attempt + 1,
                consecutive_errors=0,
                delay_seconds=120,
                provider=provider,
                session_id=session_id,
            )
            logger.info(f"[DeepResearch] In progress, attempt={attempt + 1}")
            return {"status": "polling", "attempt": attempt + 1}, 200

        if status == "completed":
            get_debug_logger().log_response(
                agent_name="deep_research",
                response=payload_text,
                metadata={"source": "gemini_polling", "interaction_id": interaction_id},
            )

            await deliver_deep_research(
                result_text=payload_text,
                user_id=user_id,
                account_id=account_id,
                query=query,
                task_queue=self._task_queue,
                session_id=session_id,
            )

            logger.info(f"[DeepResearch] Delivered to user={user_id[:8]}")
            return {"status": "delivered"}, 200

        # status == "failed"
        logger.error(
            f"[DeepResearch] Operation failed: interaction={interaction_id[:16]}, error={payload_text}"
        )
        await self._notification.notify(
            user_id=user_id, account_id=account_id,
            system_alert="Deep research did not complete — the AI provider returned an error.",
        )
        return {"status": "failed"}, 200

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
        if not self._task_setup or not self._task_queue:
            logger.warning("[Worker] renew_all_task_subscriptions: task_setup or task_queue not configured")
            return {"error": "task_setup not configured"}, 501
        user_ids = await self._oauth.list_users_by_provider("microsoft_todo")
        for user_id in user_ids:
            await self._task_queue.enqueue_worker_task(
                "renew_task_subscriptions", {"user_id": user_id}
            )
        logger.info(f"[Worker] renew_all_task_subscriptions: enqueued {len(user_ids)} renewal tasks")
        return {"status": "ok", "enqueued": len(user_ids)}, 200

    # ------------------------------------------------------------------
    # Self-reminders: fire_due_reminders
    # ------------------------------------------------------------------

    _CRON_WINDOW_SECONDS = 4 * 60  # 4 min — idempotency guard for 5-min cron

    async def _handle_fire_due_reminders(self) -> Tuple[dict, int]:
        """
        Fire all reminders with due <= now.

        For each due note:
          1. Resolve user account_id (skip if user not found).
          2. Idempotency: skip if already fired within the current cron window.
          3. Fire: notify() sends instruction to QuickAgent → delivers to user's channel.
          4. Reschedule (recurrent) or delete (one-time).

        Called by Cloud Scheduler every 15 minutes.
        """
        if not self._notes_port:
            logger.warning("[Worker] fire_due_reminders: notes_port not configured")
            return {"error": "notes_port not configured"}, 501

        now = datetime.now(timezone.utc)
        due_notes = await self._notes_port.list_due_reminders(as_of=now)
        logger.info(f"[Worker] fire_due_reminders: {len(due_notes)} due note(s) at {now.isoformat()}")

        fired, skipped = 0, 0
        for note in due_notes:
            # Idempotency: skip if fired recently (prevents double-fire on cron overlap)
            if note.last_fired and (now - note.last_fired).total_seconds() < self._CRON_WINDOW_SECONDS:
                skipped += 1
                continue

            # Resolve account_id
            user_profile = await self._user_repo.get_user(note.user_id)
            if not user_profile or not user_profile.account_id:
                logger.warning(
                    "[Worker] fire_due_reminders: user not found or no account_id: %s",
                    note.user_id[:8],
                )
                skipped += 1
                continue

            account_id = user_profile.account_id
            user_tz = ZoneInfo(user_profile.config.timezone or "UTC")

            try:
                await self._agent_factory.ensure_agents_for_user(note.user_id)
                await self._notification.notify(
                    user_id=note.user_id,
                    account_id=account_id,
                    system_alert=_build_reminder_alert(note),
                    agent_id_override=f"smart_response_agent_{note.user_id}",
                )
            except Exception as exc:
                logger.warning(
                    "[Worker] fire_due_reminders: notify failed for user=%s note=%s: %s",
                    note.user_id[:8], note.note_id, exc,
                )
                # Still reschedule/delete — notification failure is not a reason to skip.

            if note.recurrence:
                next_due = _compute_next_due(note.due, note.recurrence, user_tz)
                await self._notes_port.reschedule(note.note_id, next_due, last_fired=now)
                logger.info(
                    "[Worker] Rescheduled reminder %s → %s (user=%s)",
                    note.note_id, next_due.isoformat(), note.user_id[:8],
                )
            else:
                await self._notes_port.delete_note(note.note_id, note.user_id)
                logger.info(
                    "[Worker] Deleted one-time reminder %s (user=%s)",
                    note.note_id, note.user_id[:8],
                )

            fired += 1

        logger.info(f"[Worker] fire_due_reminders complete: fired={fired}, skipped={skipped}")
        return {"fired": fired, "skipped": skipped}, 200

    # ------------------------------------------------------------------
    # Daily email review
    # ------------------------------------------------------------------

    async def _handle_start_daily_email_review(self) -> Tuple[dict, int]:
        """
        Fan-out: enqueue daily_email_review for all Gmail users with gmail_daily_review enabled
        and whose gmail_daily_review_hour matches the current hour in their timezone.
        Called by Cloud Scheduler hourly.
        """
        if not self._oauth or not self._email_provider or not self._task_queue:
            logger.warning("[Worker] start_daily_email_review: required services not configured")
            return {"error": "services not configured"}, 501

        user_ids = await self._oauth.list_users_by_provider("gmail")
        now_utc = datetime.now(timezone.utc)
        started, skipped = 0, 0

        for user_id in user_ids:
            profile = await self._user_repo.get_user(user_id)
            if not profile:
                skipped += 1
                continue

            cfg = profile.config
            if not cfg.gmail_daily_review:
                skipped += 1
                continue

            user_tz = ZoneInfo(cfg.timezone or "UTC")
            local_hour = now_utc.astimezone(user_tz).hour
            if local_hour != cfg.gmail_daily_review_hour:
                skipped += 1
                continue

            await self._task_queue.enqueue_worker_task(
                "daily_email_review",
                {"user_id": user_id, "account_id": profile.account_id},
            )
            logger.info(f"[Worker] start_daily_email_review: enqueued for {user_id[:8]}")
            started += 1

        logger.info(f"[Worker] start_daily_email_review complete: started={started}, skipped={skipped}")
        return {"started": started, "skipped": skipped}, 200

    async def _handle_daily_email_review(self, payload: dict) -> Tuple[dict, int]:
        """
        Fetch last 24h emails for a user, build a structured payload,
        and deliver to SmartAgent for secretary-style analysis.
        SmartAgent produces an HTML page via create_html_page.
        """
        user_id = payload.get("user_id")
        account_id = payload.get("account_id")
        if not user_id or not account_id:
            return {"error": "missing user_id or account_id"}, 400

        if not self._email_provider:
            logger.warning("[Worker] daily_email_review: email_provider not configured")
            return {"error": "email_provider not configured"}, 501

        creds = await self._oauth.get_credentials(user_id, "gmail")
        if not creds:
            logger.warning(f"[Worker] daily_email_review: no credentials for {user_id[:8]}")
            return {"error": "no credentials"}, 200

        # Refresh token if expired or expiring within 5 minutes
        now_utc = datetime.now(timezone.utc)
        if creds.token_expiry and creds.token_expiry <= now_utc + timedelta(minutes=5):
            try:
                creds = await self._email_provider.refresh_token(creds)
                await self._oauth.save_credentials(creds)
            except Exception as exc:
                logger.warning(f"[Worker] daily_email_review: token refresh failed for {user_id[:8]}: {exc}")
                return {"error": "token refresh failed"}, 200

        # Fetch all emails from the last 24 hours (paginated, cap at 200)
        date_from = now_utc - timedelta(hours=24)
        all_metadata = []
        page_token = None
        _MAX_EMAILS = 200
        while len(all_metadata) < _MAX_EMAILS:
            batch, page_token = await self._email_provider.list_emails(
                credentials=creds,
                date_from=date_from,
                date_to=now_utc,
                page_token=page_token,
                max_results=min(100, _MAX_EMAILS - len(all_metadata)),
            )
            all_metadata.extend(batch)
            if not page_token:
                break

        if not all_metadata:
            logger.info(f"[Worker] daily_email_review: no emails in last 24h for {user_id[:8]}")
            return {"status": "no_emails"}, 200

        # Fetch full content (body text + attachment names, no binaries)
        email_ids = [m.email_id for m in all_metadata]
        full_content = await self._email_provider.batch_get_full_content(
            credentials=creds,
            email_ids=email_ids,
            deep=False,
        )

        # Build structured list for Smart
        _MAX_BODY_CHARS = 500
        emails_data: List[dict] = []
        for meta in all_metadata:
            content = full_content.get(meta.email_id)
            emails_data.append({
                "email_id": meta.email_id,
                "from": meta.from_address,
                "subject": meta.subject,
                "date": meta.date.isoformat(),
                "snippet": meta.snippet,
                "body": content.body_text[:_MAX_BODY_CHARS] if content and content.body_text else "",
                "attachments": content.attachments if content else [],
            })

        date_str = now_utc.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d")
        system_alert = _build_email_review_alert(date_str, emails_data)

        await self._agent_factory.ensure_agents_for_user(user_id)
        await self._notification.notify(
            user_id=user_id,
            account_id=account_id,
            system_alert=system_alert,
            agent_id_override=f"smart_response_agent_{user_id}",
        )

        logger.info(
            f"[Worker] daily_email_review: delivered {len(emails_data)} emails to Smart for {user_id[:8]}"
        )
        return {"status": "ok", "emails": len(emails_data)}, 200


# ---------------------------------------------------------------------------
# Utility: build system alert text for fired reminders
# ---------------------------------------------------------------------------

def _build_reminder_alert(note: "AgentNote") -> str:
    if note.recurrence:
        interval = note.recurrence.interval or 1
        schedule = f"this reminder recurs every {interval} {note.recurrence.type} — it will fire again on the next cycle."
    else:
        schedule = "this was a one-time reminder, it fires once."

    return (
        f'[SELF-REMINDER] "{note.text}"\n'
        f"note_id: {note.note_id}\n"
        f"This reminder was set by you for yourself at an earlier point.\n"
        f"The instruction below is your own prior intent — you wrote it then so you could act on it now.\n"
        f"Schedule: {schedule}\n"
        f"\n"
        f"{note.instruction}\n"
        f"\n"
        f"---\n"
        f"You have full context on why you set this — the instruction above is yours.\n"
        f"The manage_self_reminders intent is available if you decide to update or delete it.\n"
        f"Having received this execution context, you have an opportunity to act in the user's interest — "
        f"not only on the reminder itself, but on anything you judge valuable right now.\n"
        f"To decide what is worth doing right now, start with conversation history — "
        f"it is the primary signal: patterns, unfinished threads, recurring topics. "
        f"From there you have access to: user memory (search_memory), "
        f"active reminders (manage_self_reminders), user tasks (manage_user_tasks), "
        f"web (search_web), email archive (search_emails)."
    )


# ---------------------------------------------------------------------------
# Utility: build system alert text for daily email review
# ---------------------------------------------------------------------------

def _build_email_review_alert(date_str: str, emails: List[dict]) -> str:
    return (
        f"[DAILY EMAIL REVIEW] {date_str}\n"
        f"{len(emails)} emails received in the last 24 hours.\n"
        f"\n"
        f"{json.dumps(emails, ensure_ascii=False, indent=2)}\n"
        f"\n"
        f"---\n"
        f"This is the user's inbox for today. You know who the user is — draw on that knowledge.\n"
        f"The email_id field lets you fetch full content (get_email_details) or attachments "
        f"(get_email_attachment) for anything worth investigating further.\n"
        f"Deliver your findings as an HTML page (create_html_page)."
    )


# ---------------------------------------------------------------------------
# Utility: compute next due datetime for recurrent reminders
# ---------------------------------------------------------------------------

def _compute_next_due(
    current_due: datetime,
    recurrence: ReminderRecurrence,
    user_tz: ZoneInfo,
) -> datetime:
    """
    Compute next UTC due datetime after firing.

    - hourly: pure UTC arithmetic (DST-safe by definition)
    - daily / weekly / monthly: arithmetic in user timezone to preserve wall-clock time
      (e.g. "every day at 9am" stays at 9am local even across DST transitions)
    """
    interval = recurrence.interval or 1

    if recurrence.type == "hourly":
        return current_due + timedelta(hours=interval)

    # Convert to user's local time, compute next, convert back to UTC
    local_due = current_due.astimezone(user_tz)

    if recurrence.type == "daily":
        next_local = local_due + timedelta(days=interval)
    elif recurrence.type == "weekly":
        next_local = local_due + timedelta(weeks=interval)
    elif recurrence.type == "monthly":
        next_local = local_due + relativedelta(months=interval)
    else:
        # Unknown type — fall back to daily
        logger.warning("[compute_next_due] Unknown recurrence type %r, defaulting to daily", recurrence.type)
        next_local = local_due + timedelta(days=interval)

    return next_local.astimezone(timezone.utc)


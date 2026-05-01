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
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Optional, Tuple


if TYPE_CHECKING:
    from ..services.email_review_service import EmailReviewService
    from ..services.task_indexing_service import TaskIndexingService
    from ..services.task_setup_service import TaskSetupService
    from ..ports.agent_note_port import AgentNotePort
    from ..ports.indexed_email_repository import IndexedEmailRepository
    from ..ports.media_storage_port import MediaStoragePort
    from ..ports.account_repository import AccountRepository

from ..domain.agent import AgentIntent, AgentMessage, AgentStatus
from ..domain.notification_kind import NotificationKind
from ..handlers.agent_worker_handler import AgentWorkerHandler
from ..services.deep_research_delivery import (
    NotificationPort, deliver_deep_research,
)
from ..services.consolidation_service import ConsolidationService
from ..services.provider_registry import ProviderRegistry
from ..services.email_indexing_service import EmailIndexingService
from ..services.reminders_service import RemindersService, build_reminder_alert
from ..services.task_dispatch_service import TaskDispatchService
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
        notification_service: NotificationPort,
        consolidation_service: Optional[ConsolidationService],
        coordinator: Any,  # AgentCoordinator (avoid infrastructure import in handlers)
        agent_factory: Any,  # UserAgentFactory
        indexed_email_repo: Optional[IndexedEmailRepository],
        user_repo: Any,  # UserRepository
        task_dispatch: Optional[TaskDispatchService] = None,
        job_registry: Optional[ProviderRegistry] = None,
        media_storage: Optional[MediaStoragePort] = None,
        task_setup: "Optional[TaskSetupService]" = None,
        task_indexing: "Optional[TaskIndexingService]" = None,
        reminders_service: Optional[RemindersService] = None,
        notes_port: "Optional[AgentNotePort]" = None,
        email_review: "Optional[EmailReviewService]" = None,
        account_repo: "Optional[AccountRepository]" = None,
        billing_webhook: Any = None,  # SlackWebhookAdapter
    ) -> None:
        self._agent_worker = agent_worker_handler
        self._email_indexing = email_indexing_service
        self._notification = notification_service
        self._consolidation = consolidation_service
        self._coordinator = coordinator
        self._agent_factory = agent_factory
        self._indexed_email_repo = indexed_email_repo
        self._user_repo = user_repo
        self._task_dispatch = task_dispatch
        self._job_registry: Optional[ProviderRegistry] = job_registry
        self._media_storage = media_storage
        self._task_setup = task_setup
        self._task_indexing = task_indexing
        self._reminders_service = reminders_service
        self._notes_port = notes_port
        self._email_review = email_review
        self._account_repo = account_repo
        self._billing_webhook = billing_webhook

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
        return None  # unknown task_type — caller handles fallback

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

        # Derive origin channel from per-channel session_id (format: "user_id:channel_id")
        origin_channel_id = session_id.split(":", 1)[1] if ":" in session_id else None

        job_port = None
        if self._job_registry:
            try:
                job_port = self._job_registry.get(provider)
            except ValueError:
                logger.debug("No deep research job port registered for provider %r", provider)
        if not job_port or not self._task_dispatch:
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
                kind=NotificationKind.DEEP_RESEARCH,
                user_id=user_id,
                account_id=account_id,
                system_alert="Deep research timed out after 60 minutes without producing a result.",
                channel_id_override=origin_channel_id,
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
                    kind=NotificationKind.DEEP_RESEARCH,
                    user_id=user_id,
                    account_id=account_id,
                    system_alert="Deep research did not complete — the research session expired or encountered a persistent error.",
                    channel_id_override=origin_channel_id,
                )
                return {"status": "dead", "attempt": attempt}, 200

            await self._task_dispatch.enqueue_deep_research_polling(
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
            await self._task_dispatch.enqueue_deep_research_polling(
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
                task_queue=self._task_dispatch,
                session_id=session_id,
                channel_id_override=origin_channel_id,
            )

            logger.info(f"[DeepResearch] Delivered to user={user_id[:8]}")
            return {"status": "delivered"}, 200

        # status == "failed"
        logger.error(
            f"[DeepResearch] Operation failed: interaction={interaction_id[:16]}, error={payload_text}"
        )
        await self._notification.notify(
            kind=NotificationKind.DEEP_RESEARCH,
            user_id=user_id, account_id=account_id,
            system_alert="Deep research did not complete — the AI provider returned an error.",
            channel_id_override=origin_channel_id,
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
            # One-time fired-and-deleted, or recurrent that the user has
            # since removed. Either way: nothing to deliver, ack the task.
            logger.info(
                "[Worker] execute_reminder: note_gone note=%s user=%s",
                note_id, user_id[:8],
            )
            return {"status": "note_gone"}, 200

        # Idempotency guard against Cloud Tasks retries.
        if note.last_delivered_due is not None and note.last_delivered_due == due_at:
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
        result = await self._notification.notify(
            user_id=user_id,
            account_id=user_profile.account_id,
            system_alert=build_reminder_alert(note),
            kind=NotificationKind.REMINDER,
            agent_id_override=f"smart_response_agent_{user_id}",
            task_complexity=task_complexity,
        )

        if not result.delivered:
            logger.warning(
                "[Worker] execute_reminder: delivery_failed note=%s user=%s "
                "(agent_status=%s, error=%s)",
                note_id, user_id[:8], result.agent_status, result.error,
            )
            return {
                "error": result.error or "delivery_failed",
                "agent_status": result.agent_status.value,
            }, 500

        await self._notes_port.mark_fire_delivered(note_id=note_id, due_at=due_at)
        logger.info(
            "[Worker] execute_reminder: ok note=%s user=%s due=%s",
            note_id, user_id[:8], due_at.isoformat(),
        )
        return {"status": "ok"}, 200

    # ------------------------------------------------------------------
    # Daily email review
    # ------------------------------------------------------------------

    async def _handle_start_daily_email_review(self) -> Tuple[dict, int]:
        """
        Fan-out: enqueue daily_email_review for all Gmail users with gmail_daily_review enabled
        and whose gmail_daily_review_hour matches the current hour in their timezone.
        Called by Cloud Scheduler hourly.
        """
        if not self._email_review or not self._task_dispatch:
            logger.warning("[Worker] start_daily_email_review: required services not configured")
            return {"error": "services not configured"}, 501

        now_utc = datetime.now(timezone.utc)
        eligible = await self._email_review.find_eligible_users(self._user_repo, now_utc)

        for user_id, account_id in eligible:
            await self._task_dispatch.enqueue_worker_task(
                "daily_email_review",
                {"user_id": user_id, "account_id": account_id},
            )
            logger.info(f"[Worker] start_daily_email_review: enqueued for {user_id[:8]}")

        started = len(eligible)
        logger.info(f"[Worker] start_daily_email_review complete: started={started}")
        return {"started": started}, 200

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

        emails = await self._email_review.fetch_review_payload(user_id)
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
        )

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
        yesterday_str = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

        lines = [f"📊 *Billing Summary — {yesterday_str}*\n"]
        reported = 0

        for account in accounts:
            u = account.usage
            prev_tokens = u.prev_daily_tokens
            prev_cost = u.prev_daily_cost
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



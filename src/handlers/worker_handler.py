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
  - setup_microsoft_todo          → TaskSetupService.setup(user_id)
  - reindex_task_list             → TaskIndexingService.reindex_list(user_id, list_id)
  - renew_task_subscriptions      → TaskSetupService.renew_expiring_subscriptions(user_id)
  - renew_all_task_subscriptions  → fan-out: enqueue renew_task_subscriptions for all MS To Do users
"""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Optional, Tuple

if TYPE_CHECKING:
    from ..services.task_indexing_service import TaskIndexingService
    from ..services.task_setup_service import TaskSetupService

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
        return None  # unknown task_type — caller handles fallback

    # ------------------------------------------------------------------
    # Email indexing
    # ------------------------------------------------------------------

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
            try:
                await self._agent_factory.ensure_agents_for_user(job.user_id)
                await self._notification.notify(
                    user_id=job.user_id,
                    account_id=job.account_id,
                    system_alert=self._email_indexing.completion_alert(job),
                )
            except Exception as notify_exc:
                logger.warning(f"⚠️ [Worker] Notification failed (non-critical): {notify_exc}")

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


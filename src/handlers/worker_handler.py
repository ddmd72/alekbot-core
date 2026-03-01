"""
Worker Handler
==============

Dispatches Cloud Tasks payloads to the appropriate handler by task_type.
Extracted from main.py for testability and separation of concerns.

Supported task_types:
  - agent_execution       → AgentWorkerHandler
  - email_indexing        → run one indexing page, re-enqueue if more
  - email_indexing_watchdog → mark stale running jobs as failed
  - consolidation         → process one batch, re-enqueue if more
"""

from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from ..handlers.agent_worker_handler import AgentWorkerHandler
from ..handlers.consolidation_handler import process_user_batches_on_overflow
from ..ports.consolidation_queue import ConsolidationQueue
from ..ports.email_indexing_job_repository import EmailIndexingJobRepository
from ..ports.indexed_email_repository import IndexedEmailRepository
from ..ports.oauth_credentials_port import OAuthCredentialsPort
from ..ports.task_queue import TaskQueue
from ..services.email_indexing_service import EmailIndexingService
from ..services.user_notification_service import UserNotificationService
from ..utils.logger import logger


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
        notification_service: UserNotificationService,
        consolidation_queue: ConsolidationQueue,
        coordinator: Any,  # AgentCoordinator (avoid infrastructure import in handlers)
        agent_factory: Any,  # UserAgentFactory
        indexed_email_repo: Optional[IndexedEmailRepository],
        user_repo: Any,  # UserRepository
        task_queue: Optional[TaskQueue] = None,
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

    async def handle(self, payload: dict) -> Optional[Tuple[dict, int]]:
        """
        Dispatch to appropriate handler by task_type.

        Returns (body_dict, status_code) for known task_types, or None for
        unknown types (caller should handle fallback).
        """
        task_type = payload.get("task_type")
        if task_type == "agent_execution":
            result = await self._agent_worker.handle_task(payload)
            return result, 200
        elif task_type == "email_indexing":
            return await self._handle_email_indexing(payload)
        elif task_type == "email_indexing_watchdog":
            return await self._handle_watchdog()
        elif task_type == "consolidation":
            return await self._handle_consolidation(payload)
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

        job = await self._email_indexing.run_indexing_job(
            job=job,
            credentials=creds,
            account_id=job.account_id,
            max_pages=1,
            mode=job.mode,
            backfill_until=job.backfill_until,
        )

        if job.next_page_token and self._task_queue:
            await self._task_queue.enqueue_email_indexing_task(job.job_id)
            logger.info(f"📬 [Worker] Re-enqueued email indexing page for job {job.job_id[:8]}")
        elif not job.next_page_token:
            try:
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

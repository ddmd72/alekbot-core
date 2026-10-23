"""
ConsolidationService
====================

Owns consolidation batch processing. Extracted from handlers/consolidation_handler.py
so that ConsolidationQueue is accessed through a service rather than by handlers.

WorkerHandler and ConversationHandler receive ConsolidationService via constructor
injection (`process_user_batches`, `consolidate_now`) — they never import ConsolidationQueue.

One batch at a time per account: each batch runs under the account's memory-write lease
(FACT_LIFECYCLE_RFC D9). A run that finds the lease taken defers itself with a named task.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional

from ..domain.agent import AgentMessage, AgentIntent, AgentStatus
from ..domain.consolidation import MAX_FAILED_REVIVALS, BatchStatus, ConsolidationBatch
from ..domain.consolidation_run import ConsolidationRun
from ..domain.memory_write_lease import (
    LEASE_BUSY_RETRY_DELAY_S,
    LEASE_MARGIN,
    agent_timeout_s,
    defer_bucket,
    lease_until,
)
from ..domain.request_context import RequestContext
from ..ports.consolidation_queue import ConsolidationQueue
from ..ports.memory_write_lease import MemoryWriteLeasePort
from ..ports.task_queue import TaskQueue
from ..utils.logger import logger

_INTENT_CONSOLIDATE_FULL = "consolidate_full"  # mirrors Intent.CONSOLIDATE_FULL
_NO_ACCOUNT_ERROR = "user has no account"
_DEFERRAL_DEADLINE_S = 1800  # same Cloud Tasks dispatch deadline as enqueue_consolidation_task
_MARGIN_S = LEASE_MARGIN.total_seconds()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _Step(Enum):
    """How one leased attempt at one batch ended."""

    CONSOLIDATED = "consolidated"
    FAILED = "failed"
    NOTHING_PENDING = "nothing_pending"
    NO_TIME = "no_time"
    DEFERRED = "deferred"
    ERROR = "error"


class ConsolidationService:
    """
    Processes pending consolidation batches for a user, one leased batch at a time.

    max_batches=1 (worker and `$consolidate`): one batch per HTTP request;
    the caller continues with another task when has_more=True.
    max_batches=None: process all pending batches.
    """

    def __init__(
        self,
        queue: ConsolidationQueue,
        coordinator: Any,  # AgentCoordinator — typed as Any to avoid infrastructure import
        agent_factory: Any,  # UserAgentFactory
        lease: MemoryWriteLeasePort,
        run_timeout_s: float,
        task_queue: Optional[TaskQueue] = None,
        indexed_email_repo: Optional[object] = None,
        user_repo: Optional[object] = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._queue = queue
        self._coordinator = coordinator
        self._agent_factory = agent_factory
        self._lease = lease
        self._run_timeout_s = run_timeout_s
        self._task_queue = task_queue
        self._indexed_email_repo = indexed_email_repo
        self._user_repo = user_repo
        self._clock = clock

    async def find_stuck_users(self) -> list[str]:
        """Distinct user_ids with unconsolidated batches still in the queue.

        Used by the hourly sweep scheduler to re-trigger consolidation for users whose
        batches stalled (e.g. provider billing exhaustion) instead of waiting for the
        next session overflow. Delegates straight to the queue — no per-user work here.
        """
        return await self._queue.get_stuck_batch_user_ids()

    async def revive_failed_batches(self, user_id: str) -> int:
        """Sweep only: turn this user's FAILED batches back into RETRY_PENDING (bounded).

        Each batch is revived at most MAX_FAILED_REVIVALS times in its life. The per-task path in
        `process_user_batches` never does this: it re-enqueues itself, so reviving there would retry
        a failing batch back to back without end.
        """
        return await self._queue.reset_recoverable_batches(user_id, revive_failed=True)

    async def process_user_batches(
        self,
        user_id: str,
        max_batches: Optional[int] = None,
    ) -> bool:
        """
        Process pending consolidation batches for a specific user (the worker path).

        Returns has_more: True if pending batches remain after this call. A deferral returns False:
        the named deferral task is the continuation.
        """
        return (await self._run(user_id, max_batches)).has_more

    async def consolidate_now(self, user_id: str) -> ConsolidationRun:
        """
        The `$consolidate` path: at most one batch inside the request (the chat task has the same
        1800 s deadline as a worker task), then the continuation goes to the queue.
        """
        run = await self._run(user_id, max_batches=1)
        if run.has_more:
            if self._task_queue is None:
                logger.warning(
                    "[Librarian] no queue for the continuation: more batches wait for user %s "
                    "until the next sweep", user_id[:8],
                )
            else:
                await self._task_queue.enqueue_consolidation_task(user_id=user_id)
        return run

    async def _run(self, user_id: str, max_batches: Optional[int]) -> ConsolidationRun:
        logger.info(
            "👨‍🏫 [Librarian] Starting overflow consolidation for user %s... (max_batches=%s)",
            user_id[:8], max_batches,
        )

        account_id = await self._account_to_lease(user_id)
        if account_id is None:
            return ConsolidationRun()

        processed = 0
        while max_batches is None or processed < max_batches:
            step = await self._consolidate_one(user_id, account_id)
            if step is _Step.CONSOLIDATED:
                processed += 1
            elif step is _Step.DEFERRED:
                return ConsolidationRun(processed=processed, deferred=True)
            elif step is _Step.ERROR:
                return ConsolidationRun(processed=processed)
            else:
                break

        has_more = await self._has_pending(user_id)
        if has_more:
            logger.info("📬 [Librarian] More pending batches remain for user %s", user_id[:8])
        return ConsolidationRun(processed=processed, has_more=has_more)

    async def _account_to_lease(self, user_id: str) -> Optional[str]:
        """The user's account, or None when the run stops here. A setup error is logged and swallowed, as it
        always was; a blank account fails the oldest batch instead of leaving a 5xx retry loop."""
        try:
            await self._agent_factory.ensure_agents_for_user(user_id)

            user_profile = await self._user_repo.get_user(user_id) if self._user_repo else None
            account_id = user_profile.account_id if user_profile else user_id
            if not account_id:
                await self._fail_oldest_batch_without_account(user_id)
                return None
            return account_id
        except Exception as exc:
            self._log_unhandled(user_id, exc)
            return None

    async def _has_pending(self, user_id: str) -> bool:
        try:
            return len(await self._queue.get_pending_batches(user_id=user_id, limit=1)) > 0
        except Exception as exc:
            self._log_unhandled(user_id, exc)
            return False

    @staticmethod
    def _log_unhandled(user_id: str, exc: Exception) -> None:
        logger.error(
            "❌ [Librarian] Unhandled error in batch processing for %s: %s",
            user_id, exc, exc_info=True,
        )

    async def _consolidate_one(self, user_id: str, account_id: str) -> _Step:
        """One batch under a fresh lease. Lease and deferral-queue errors propagate; a release error does not."""
        owner = uuid.uuid4().hex
        now = self._clock()
        until = lease_until(now, self._run_timeout_s)
        if not await self._lease.acquire(account_id, owner, now, until):
            if not await self._queue.has_open_batches(user_id):
                return _Step.NOTHING_PENDING
            await self._defer(user_id, now, "lease busy")
            return _Step.DEFERRED
        try:
            step = await self._consolidate_leased(user_id, account_id, until)
            if step is _Step.NO_TIME:
                await self._defer(user_id, now, "less than the lease margin left")
                return _Step.DEFERRED
            return step
        finally:
            await self._release(account_id, owner)

    async def _consolidate_leased(self, user_id: str, account_id: str, until: datetime) -> _Step:
        try:
            async with RequestContext(user_id=user_id, account_id=account_id):
                logger.debug(
                    "✅ RequestContext set: user_id=%s, account_id=%s",
                    user_id[:8], account_id[:12],
                )
                return await self._process_one_batch(user_id, until)
        except Exception as exc:
            self._log_unhandled(user_id, exc)
            return _Step.ERROR

    async def _process_one_batch(self, user_id: str, until: datetime) -> _Step:
        await self._queue.reset_recoverable_batches(user_id, zombie_after_s=_MARGIN_S)

        batches = await self._queue.get_pending_batches(user_id=user_id, limit=1)
        if not batches:
            logger.debug("✅ No more pending batches for user %s", user_id[:8])
            return _Step.NOTHING_PENDING

        batch = batches[0]
        if agent_timeout_s(until, self._clock()) < _MARGIN_S:
            return _Step.NO_TIME

        logger.info(
            "📦 [Librarian] Processing batch %s (%d messages)",
            batch.batch_id, len(batch.messages),
        )

        await self._queue.update_batch_status(batch.batch_id, BatchStatus.PROCESSING)

        message = AgentMessage(
            task_id=batch.batch_id,
            sender="consolidation_service",
            recipient=f"consolidation_agent_{batch.user_id}",
            intent=AgentIntent.DELEGATE,
            payload={
                "task": _INTENT_CONSOLIDATE_FULL,
                "batch_id": batch.batch_id,
                "messages": batch.messages,
            },
            context={"user_id": batch.user_id, "suppress_transient_retry": True},
            timeout_ms=int(agent_timeout_s(until, self._clock()) * 1000),
        )

        response = await self._coordinator.route_message(message)

        if response.status == AgentStatus.SUCCESS:
            await self._queue.delete_batch(batch.batch_id)
            result = response.result or {}
            logger.info(
                "✅ [Librarian] Batch %s consolidated: stage1_ops=%d, email_batches=%d → DELETED",
                batch.batch_id,
                result.get("stage1_operations", 0),
                result.get("email_batches", 0),
            )
            return _Step.CONSOLIDATED

        attempts = await self._queue.increment_attempts(batch.batch_id)
        error_str = str(response.error)
        if attempts >= 3:
            await self._mark_failed(batch, attempts, error_str)
        else:
            await self._queue.update_batch_status(
                batch.batch_id, BatchStatus.RETRY_PENDING, error=error_str
            )
            logger.warning(
                "⚠️ [Librarian] Batch %s failed (attempt %d/3) user=%s: %s",
                batch.batch_id, attempts, batch.user_id[:8], error_str,
            )
        return _Step.FAILED

    async def _mark_failed(self, batch: ConsolidationBatch, attempts: int, error_str: str) -> None:
        await self._queue.update_batch_status(batch.batch_id, BatchStatus.FAILED, error=error_str)
        # Single, structured ERROR line — alert/dashboard friendly.
        # Keep all signals on one line: batch_id, user_id, attempts,
        # message_count, full error string. Grep target:
        # "[Librarian] CONSOLIDATION_FAILED".
        logger.error(
            "❌ [Librarian] CONSOLIDATION_FAILED batch_id=%s user_id=%s "
            "attempts=%d messages=%d error=%r",
            batch.batch_id, batch.user_id, attempts,
            len(batch.messages), error_str,
        )
        if batch.revivals >= MAX_FAILED_REVIVALS:
            # Last allowed round failed: the sweep will not revive it again.
            # Messages stay in the queue document. Grep target:
            # "[Librarian] CONSOLIDATION_GAVE_UP".
            logger.error(
                "❌ [Librarian] CONSOLIDATION_GAVE_UP batch_id=%s user_id=%s "
                "revivals=%d messages=%d (kept in the queue, not revived again)",
                batch.batch_id, batch.user_id[:8], batch.revivals,
                len(batch.messages),
            )

    async def _fail_oldest_batch_without_account(self, user_id: str) -> None:
        """No account to lease: fail the batch instead of leaving a 5xx retry loop (nothing can be written)."""
        batches = await self._queue.get_pending_batches(user_id=user_id, limit=1)
        if batches:
            await self._mark_failed(batches[0], batches[0].attempts, _NO_ACCOUNT_ERROR)

    async def _defer(self, user_id: str, now: datetime, reason: str) -> None:
        """One task per user and bucket, so concurrent losers share it and a loser that loses again lands later."""
        if self._task_queue is None:
            logger.warning("[Librarian] %s, no queue to defer to (user %s)", reason, user_id[:8])
            return
        await self._task_queue.enqueue_worker_task(
            "consolidation",
            {"user_id": user_id},
            delay_seconds=LEASE_BUSY_RETRY_DELAY_S,
            deadline_seconds=_DEFERRAL_DEADLINE_S,
            dedup_key=f"consolidation-defer:{user_id}:{defer_bucket(now)}",
        )
        logger.info("⏳ [Librarian] %s: deferred consolidation for user %s", reason, user_id[:8])

    async def _release(self, account_id: str, owner: str) -> None:
        try:
            await self._lease.release(account_id, owner)
        except Exception as exc:
            logger.warning(
                "[Librarian] releasing the memory-write lease failed for account %s (it expires on its own): %s",
                account_id[:8], exc,
            )

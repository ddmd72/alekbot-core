"""
ConsolidationService
====================

Owns consolidation batch processing. Extracted from handlers/consolidation_handler.py
so that ConsolidationQueue is accessed through a service rather than by handlers.

WorkerHandler and ConversationHandler receive ConsolidationService via constructor
injection and call process_user_batches() — they never import ConsolidationQueue.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from ..domain.agent import AgentMessage, AgentIntent, AgentStatus
from ..domain.consolidation import BatchStatus
from ..ports.consolidation_queue import ConsolidationQueue
from ..utils.logger import logger

_INTENT_CONSOLIDATE_FULL = "consolidate_full"  # mirrors Intent.CONSOLIDATE_FULL


class ConsolidationService:
    """
    Processes pending consolidation batches for a user.

    max_batches=1 (overflow path): one batch per Cloud Tasks HTTP request;
    caller re-enqueues another task when has_more=True.
    max_batches=None (manual $consolidate): process all pending batches.
    """

    def __init__(
        self,
        queue: ConsolidationQueue,
        coordinator: Any,  # AgentCoordinator — typed as Any to avoid infrastructure import
        agent_factory: Any,  # UserAgentFactory
        indexed_email_repo: Optional[object] = None,
        user_repo: Optional[object] = None,
    ) -> None:
        self._queue = queue
        self._coordinator = coordinator
        self._agent_factory = agent_factory
        self._indexed_email_repo = indexed_email_repo
        self._user_repo = user_repo

    async def find_stuck_users(self) -> list[str]:
        """Distinct user_ids with unconsolidated batches still in the queue.

        Used by the hourly sweep scheduler to re-trigger consolidation for users whose
        batches stalled (e.g. provider billing exhaustion) instead of waiting for the
        next session overflow. Delegates straight to the queue — no per-user work here.
        """
        return await self._queue.get_stuck_batch_user_ids()

    async def process_user_batches(
        self,
        user_id: str,
        max_batches: Optional[int] = None,
    ) -> bool:
        """
        Process pending consolidation batches for a specific user.

        Returns has_more: True if pending batches remain after this call.
        """
        logger.info(
            "👨‍🏫 [Librarian] Starting overflow consolidation for user %s... (max_batches=%s)",
            user_id[:8], max_batches,
        )

        try:
            await self._agent_factory.ensure_agents_for_user(user_id)

            user_profile = await self._user_repo.get_user(user_id) if self._user_repo else None
            account_id = user_profile.account_id if user_profile else user_id

            from ..domain.request_context import RequestContext
            async with RequestContext(user_id=user_id, account_id=account_id):
                logger.debug(
                    "✅ RequestContext set: user_id=%s, account_id=%s",
                    user_id[:8], (account_id[:12] if account_id else "None"),
                )

                await self._queue.reset_recoverable_batches(user_id)

                processed = 0
                while True:
                    if max_batches is not None and processed >= max_batches:
                        break

                    batches = await self._queue.get_pending_batches(user_id=user_id, limit=1)
                    if not batches:
                        logger.debug("✅ No more pending batches for user %s", user_id[:8])
                        break

                    batch = batches[0]
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
                        context={"user_id": batch.user_id},
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
                        processed += 1
                    else:
                        attempts = await self._queue.increment_attempts(batch.batch_id)
                        error_str = str(response.error)
                        if attempts >= 3:
                            await self._queue.update_batch_status(
                                batch.batch_id, BatchStatus.FAILED, error=error_str
                            )
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
                        else:
                            await self._queue.update_batch_status(
                                batch.batch_id, BatchStatus.RETRY_PENDING, error=error_str
                            )
                            logger.warning(
                                "⚠️ [Librarian] Batch %s failed (attempt %d/3) user=%s: %s",
                                batch.batch_id, attempts, batch.user_id[:8], error_str,
                            )
                        break

            remaining = await self._queue.get_pending_batches(user_id=user_id, limit=1)
            has_more = len(remaining) > 0
            if has_more:
                logger.info("📬 [Librarian] More pending batches remain for user %s", user_id[:8])
            return has_more

        except Exception as exc:
            logger.error(
                "❌ [Librarian] Unhandled error in batch processing for %s: %s",
                user_id, exc, exc_info=True,
            )
            return False

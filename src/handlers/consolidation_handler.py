"""
Consolidation process handler (The Librarian).
Extracted from slack_handler.py for reusability across platforms.
"""
import uuid
import asyncio
from typing import List, Optional, TYPE_CHECKING

from ..domain.agent import AgentMessage, AgentIntent, AgentStatus
from ..infrastructure.agent_manifest import Intent
from ..domain.consolidation import BatchStatus
from ..infrastructure.agent_coordinator import AgentCoordinator
from ..ports.consolidation_queue import ConsolidationQueue
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..composition.user_agent_factory import UserAgentFactory



async def process_user_batches_on_overflow(
    user_id: str,
    coordinator: AgentCoordinator,
    agent_factory: "UserAgentFactory",
    queue: ConsolidationQueue,
    max_batches: Optional[int] = None,
    indexed_email_repo=None,
    user_repo=None,
) -> bool:
    """
    Process pending consolidation batches for a specific user.

    Args:
        max_batches: Maximum number of batches to process in this call.
                     None = unlimited (used by manual $consolidate command).
                     1 = one batch per Cloud Tasks HTTP request (overflow path) —
                     caller re-enqueues another task if has_more is True.

    Returns:
        has_more: True if there are still pending/retry_pending batches after this call.

    SESSION_27: Establishes RequestContext for implicit multi-tenant operations.
    """
    logger.info(f"👨‍🏫 [Librarian] Starting overflow consolidation for user {user_id[:8]}... (max_batches={max_batches})")

    try:
        await agent_factory.ensure_agents_for_user(user_id)

        # SESSION_27: Get account_id for RequestContext
        user_profile = await user_repo.get_user(user_id)
        account_id = user_profile.account_id if user_profile else user_id

        # SESSION_27: Establish RequestContext for all consolidation operations
        from ..domain.request_context import RequestContext
        async with RequestContext(user_id=user_id, account_id=account_id):
            logger.debug(f"✅ RequestContext set: user_id={user_id[:8]}, account_id={account_id[:12] if account_id else 'None'}")

            # Recovery: reset any PROCESSING zombies left by previous crashed/throttled workers.
            # Moves stale PROCESSING → RETRY_PENDING so get_pending_batches can pick them up.
            await queue.reset_processing_batches(user_id)

            # Process batches sequentially to maintain order and avoid race conditions.
            # max_batches=1 (overflow path): process one batch per Cloud Tasks HTTP request;
            # caller re-enqueues a new task when has_more=True.
            # max_batches=None (manual $consolidate): process all pending batches in one request.
            processed = 0
            while True:
                if max_batches is not None and processed >= max_batches:
                    break

                batches = await queue.get_pending_batches(user_id=user_id, limit=1)
                if not batches:
                    logger.debug(f"✅ No more pending batches for user {user_id[:8]}")
                    break

                batch = batches[0]
                logger.info(f"📦 [Librarian] Processing batch {batch.batch_id} ({len(batch.messages)} messages)")

                # Mark as processing
                await queue.update_batch_status(batch.batch_id, BatchStatus.PROCESSING)

                message = AgentMessage(
                    task_id=batch.batch_id,
                    sender="consolidation_handler",
                    recipient=f"consolidation_agent_{batch.user_id}",
                    intent=AgentIntent.DELEGATE,
                    payload={
                        "task": Intent.CONSOLIDATE_FULL,
                        "batch_id": batch.batch_id,
                        "messages": batch.messages,
                    },
                    context={"user_id": batch.user_id}
                )

                response = await coordinator.route_message(message)

                if response.status == AgentStatus.SUCCESS:
                    # Success -> Delete batch (Sliding Window v6 protocol)
                    await queue.delete_batch(batch.batch_id)

                    result = response.result or {}
                    logger.info(
                        f"✅ [Librarian] Batch {batch.batch_id} consolidated: "
                        f"stage1_ops={result.get('stage1_operations', 0)}, "
                        f"email_batches={result.get('email_batches', 0)} → DELETED"
                    )
                    processed += 1
                else:
                    # Failure -> Increment attempts and set to RETRY_PENDING or FAILED
                    attempts = await queue.increment_attempts(batch.batch_id)

                    if attempts >= 3:
                        await queue.update_batch_status(
                            batch.batch_id,
                            BatchStatus.FAILED,
                            error=str(response.error)
                        )
                        logger.error(f"❌ [Librarian] Batch {batch.batch_id} failed after {attempts} attempts. Skipping.")
                    else:
                        await queue.update_batch_status(
                            batch.batch_id,
                            BatchStatus.RETRY_PENDING,
                            error=str(response.error)
                        )
                        logger.warning(f"⚠️ [Librarian] Batch {batch.batch_id} failed (attempt {attempts}). Set to retry later.")

                    # Stop processing on first failure to maintain queue order
                    break

        # Check whether there are still pending batches (used by caller to decide re-enqueue)
        remaining = await queue.get_pending_batches(user_id=user_id, limit=1)
        has_more = len(remaining) > 0
        if has_more:
            logger.info(f"📬 [Librarian] More pending batches remain for user {user_id[:8]}")
        return has_more

    except Exception as e:
        logger.error(f"❌ [Librarian] Unhandled error in batch processing for {user_id}: {e}", exc_info=True)
        return False

async def _execute_consolidation_background(
    coordinator: AgentCoordinator,
    agent_factory: "UserAgentFactory",
    user_id: str,
    indexed_email_repo=None,
    user_repo=None,
) -> None:
    """
    Background task for consolidation execution.
    Runs without blocking the user's command response.

    SESSION 2026-02-07: Added RequestContext for implicit multi-tenant operations.
    """
    try:
        logger.info(f"👨‍🏫 [Librarian] Background consolidation started for user {user_id}...")

        await agent_factory.ensure_agents_for_user(user_id)

        # SESSION 2026-02-07: Get account_id for RequestContext
        user_profile = await user_repo.get_user(user_id)
        account_id = user_profile.account_id if user_profile else user_id

        # SESSION 2026-02-07: Establish RequestContext for all consolidation operations
        from ..domain.request_context import RequestContext
        async with RequestContext(user_id=user_id, account_id=account_id):
            logger.debug(f"✅ RequestContext set: user_id={user_id[:8]}, account_id={account_id[:12] if account_id else 'None'}")

            message = AgentMessage.create(
                sender="consolidation_handler",
                recipient=f"consolidation_agent_{user_id}",
                intent=AgentIntent.DELEGATE,
                payload={
                    "task": Intent.CONSOLIDATE_FULL,
                },
                context={
                    "user_id": user_id,
                }
            )

            response = await coordinator.route_message(message)

            if response.status != AgentStatus.SUCCESS:
                logger.error(f"❌ [Librarian] Background consolidation failed: {response.error}")
            else:
                result = response.result or {}
                logger.info(
                    f"✅ [Librarian] Background consolidation completed: "
                    f"stage1_ops={result.get('stage1_operations', 0)}, "
                    f"email_batches={result.get('email_batches', 0)}"
                )

    except Exception as e:
        logger.error(f"❌ [Librarian] Background consolidation error: {e}", exc_info=True)

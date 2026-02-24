"""
Consolidation process handler (The Librarian).
Extracted from slack_handler.py for reusability across platforms.
"""
import re
import json
import asyncio
from typing import List, Dict

from ..domain.messaging import ResponseChannel
from ..domain.agent import AgentMessage, AgentIntent, AgentStatus
from ..domain.consolidation import BatchStatus
from ..infrastructure.agent_coordinator import AgentCoordinator
from ..services.user_agent_factory import UserAgentFactory
from ..ports.consolidation_queue import ConsolidationQueue
from ..utils.logger import logger


def _sanitize_llm_ids(items: List[Dict]) -> List[Dict]:
    """
    Sanitizes a list of items from LLM to ensure ID uniqueness within the batch.

    Args:
        items: List of items with 'id' fields

    Returns:
        Sanitized list with unique IDs
    """
    seen_ids = set()
    sanitized_items = []

    for item in items:
        original_id = item.get('id')
        if not original_id:
            continue

        new_id = original_id
        suffix_counter = 0

        while new_id in seen_ids:
            suffix_counter += 1
            new_id = f"{original_id}_{chr(96 + suffix_counter)}"

        if new_id != original_id:
            logger.warning(f"Sanitizing duplicate LLM-generated ID: '{original_id}' -> '{new_id}'")
            item['id'] = new_id

        seen_ids.add(new_id)
        sanitized_items.append(item)

    return sanitized_items


async def process_user_batches_on_overflow(
    user_id: str,
    coordinator: AgentCoordinator,
    agent_factory: UserAgentFactory,
    queue: ConsolidationQueue
) -> None:
    """
    Process ALL pending consolidation batches for a specific user.
    Triggered by the overflow event (Librarian process).

    SESSION_27: Establishes RequestContext for implicit multi-tenant operations.
    """
    logger.info(f"👨‍🏫 [Librarian] Starting overflow consolidation for user {user_id[:8]}...")

    try:
        await agent_factory.ensure_agents_for_user(user_id)

        # SESSION_27: Get account_id for RequestContext
        user_profile = await agent_factory.user_repo.get_user(user_id)
        account_id = user_profile.account_id if user_profile else user_id

        # SESSION_27: Establish RequestContext for all consolidation operations
        from ..domain.request_context import RequestContext
        async with RequestContext(user_id=user_id, account_id=account_id):
            logger.debug(f"✅ RequestContext set: user_id={user_id[:8]}, account_id={account_id[:12] if account_id else 'None'}")

            # Get all pending or retry_pending batches for this user
            # We process them sequentially to maintain order and avoid race conditions
            while True:
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
                        "task": "consolidate",
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
                    try:
                        new_facts = result.get('new_facts', 0) if isinstance(result, dict) else 0
                        new_anchors = result.get('new_anchors', 0) if isinstance(result, dict) else 0
                        facts_extracted = int(new_facts) + int(new_anchors)
                    except (ValueError, TypeError):
                        facts_extracted = 0

                    logger.info(f"✅ [Librarian] Batch {batch.batch_id} consolidated: {facts_extracted} facts → DELETED")
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

                    # Stop processing current user's queue on first failure to maintain order
                    break

    except Exception as e:
        logger.error(f"❌ [Librarian] Unhandled error in batch processing for {user_id}: {e}", exc_info=True)

async def _execute_consolidation_background(
    coordinator: AgentCoordinator,
    agent_factory: UserAgentFactory,
    user_id: str
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
        user_profile = await agent_factory.user_repo.get_user(user_id)
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
                    "task": "consolidate"
                },
                context={
                    "user_id": user_id
                }
            )

            response = await coordinator.route_message(message)

            if response.status != AgentStatus.SUCCESS:
                logger.error(f"❌ [Librarian] Background consolidation failed: {response.error}")
            else:
                result = response.result or {}
                logger.info(
                    f"✅ [Librarian] Background consolidation completed: "
                    f"facts={result.get('new_facts', 0)}, anchors={result.get('new_anchors', 0)}"
                )

    except Exception as e:
        logger.error(f"❌ [Librarian] Background consolidation error: {e}", exc_info=True)


async def run_consolidation_process(
    response_channel: ResponseChannel,
    coordinator: AgentCoordinator,
    agent_factory: UserAgentFactory,
    user_id: str
) -> None:
    """
    The main logic for the Librarian's consolidation process.
    Sends immediate response and runs consolidation, keeping the HTTP request alive
    so Cloud Run allocates full CPU throughout.

    Args:
        response_channel: Channel for sending status updates
        coordinator: AgentCoordinator instance
        agent_factory: UserAgentFactory for per-user agents
        user_id: ID of the user initiating consolidation
    """
    try:
        logger.info(f"👨‍🏫 [Librarian] Starting consolidation process for user {user_id}...")

        # Send immediate response to user
        await response_channel.send_message("👨‍🏫 *Бібліотекар за роботою...* Починаю консолідацію спостережень.")

        # Await consolidation — keeps the HTTP request alive → full CPU on Cloud Run
        await _execute_consolidation_background(coordinator, agent_factory, user_id)

    except Exception as e:
        logger.error(f"❌ [Librarian] Error: {e}", exc_info=True)
        await response_channel.send_message(f"❌ *Помилка:* `{e}`")

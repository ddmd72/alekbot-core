"""
Consolidation process handler (The Librarian).
Extracted from slack_handler.py for reusability across platforms.
"""
import re
import json
import uuid
import asyncio
from datetime import datetime
from typing import List, Dict, Optional

from ..domain.agent import AgentMessage, AgentIntent, AgentStatus
from ..domain.consolidation import BatchStatus
from ..domain.email import IndexedEmail
from ..infrastructure.agent_coordinator import AgentCoordinator
from ..composition.user_agent_factory import UserAgentFactory
from ..ports.consolidation_queue import ConsolidationQueue
from ..utils.logger import logger

_EMAIL_TRIAGE_BATCH_SIZE = 200
_EMAIL_TRIAGE_PASSES = 2  # passes per consolidation trigger (up to 400 email facts)


def _format_email_candidates(emails: List[IndexedEmail]) -> str:
    """Format indexed email facts as numbered JSON candidates for ConsolidationAgent system_alert."""
    lines = []
    for i, email in enumerate(emails, 1):
        candidate: Dict = {
            "email_id": email.email_id,
            "fact": email.text,
            "category": email.category,
            "tags": email.tags,
            "date": email.email_date.strftime("%Y-%m-%d"),
            "from": email.from_address,
            "subject": email.subject,
        }
        if email.attachments:
            candidate["attachments"] = email.attachments
        if email.metadata:
            candidate["metadata"] = email.metadata
        lines.append(f"{i}. {json.dumps(candidate, ensure_ascii=False)}")
    return "\n".join(lines)


async def _run_email_triage_pass(
    user_id: str,
    account_id: str,
    coordinator: AgentCoordinator,
    indexed_email_repo,
) -> bool:
    """
    Run one email triage pass: fetch up to _EMAIL_TRIAGE_BATCH_SIZE unconsolidated facts,
    send to ConsolidationAgent, mark consolidated.

    Returns True if there are still more unconsolidated facts after this pass.
    See docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md §13.1.
    """
    emails = await indexed_email_repo.get_unconsolidated_batch(
        user_id, limit=_EMAIL_TRIAGE_BATCH_SIZE
    )
    if not emails:
        return False

    logger.info(
        f"📧 [Librarian] Email triage pass: {len(emails)} candidates for user {user_id[:8]}"
    )

    system_alert = (
        "[system_alert] The system has scanned the user's email inbox on their behalf "
        "and selected candidates for inclusion in the fact database. "
        "The selection contains noise. Evaluate the incoming data and process it "
        "according to your algorithm.\n\n"
        f"Candidates:\n{_format_email_candidates(emails)}"
    )

    message = AgentMessage(
        task_id=str(uuid.uuid4()),
        sender="consolidation_handler",
        recipient=f"consolidation_agent_{user_id}",
        intent=AgentIntent.DELEGATE,
        payload={
            "task": "consolidate",
            "messages": [{"role": "user", "text": system_alert}],
        },
        context={"user_id": user_id, "account_id": account_id},
    )

    response = await coordinator.route_message(message)

    if response.status == AgentStatus.SUCCESS:
        now = datetime.utcnow()
        email_ids = [e.email_id for e in emails]
        await indexed_email_repo.mark_consolidated(user_id, email_ids, now)
        result = response.result or {}
        logger.info(
            f"✅ [Librarian] Email triage pass done: {len(emails)} candidates → "
            f"{result.get('facts_affected', 0)} facts affected"
        )
        # Return True if batch was full (likely more remain)
        return len(emails) == _EMAIL_TRIAGE_BATCH_SIZE
    else:
        logger.error(
            f"❌ [Librarian] Email triage failed: {response.error}. "
            f"Emails will be retried on next consolidation."
        )
        return False


async def _run_email_triage(
    user_id: str,
    account_id: str,
    coordinator: AgentCoordinator,
    indexed_email_repo,
) -> None:
    """
    Run up to _EMAIL_TRIAGE_PASSES email triage passes per consolidation trigger.
    Each pass processes up to _EMAIL_TRIAGE_BATCH_SIZE facts (default 2 × 200 = 400).
    """
    if indexed_email_repo is None:
        return
    for pass_num in range(1, _EMAIL_TRIAGE_PASSES + 1):
        logger.info(f"📧 [Librarian] Email triage pass {pass_num}/{_EMAIL_TRIAGE_PASSES}")
        has_more = await _run_email_triage_pass(user_id, account_id, coordinator, indexed_email_repo)
        if not has_more:
            break


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
        _user_repo = user_repo or agent_factory.user_repo
        user_profile = await _user_repo.get_user(user_id)
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

            # Email triage: up to _EMAIL_TRIAGE_PASSES passes after conversation batches
            _email_repo = indexed_email_repo or getattr(agent_factory, "indexed_email_repo", None)
            await _run_email_triage(
                user_id=user_id,
                account_id=account_id,
                coordinator=coordinator,
                indexed_email_repo=_email_repo,
            )

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
    agent_factory: UserAgentFactory,
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
        _user_repo = user_repo or agent_factory.user_repo
        user_profile = await _user_repo.get_user(user_id)
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

            # Email triage: up to _EMAIL_TRIAGE_PASSES passes after conversation consolidation
            _email_repo = indexed_email_repo or getattr(agent_factory, "indexed_email_repo", None)
            await _run_email_triage(
                user_id=user_id,
                account_id=account_id,
                coordinator=coordinator,
                indexed_email_repo=_email_repo,
            )

    except Exception as e:
        logger.error(f"❌ [Librarian] Background consolidation error: {e}", exc_info=True)

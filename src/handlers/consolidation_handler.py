"""
Consolidation process handler (The Librarian).

Batch processing logic moved to src/services/consolidation_service.py (2026-03-29).
This module keeps backward-compatible shims for the composition layer until it
migrates to ConsolidationService directly.
"""
from __future__ import annotations

from typing import Any, Optional, TYPE_CHECKING

from ..infrastructure.agent_coordinator import AgentCoordinator
from ..services.consolidation_service import ConsolidationService  # noqa: F401
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..composition.user_agent_factory import UserAgentFactory


async def process_user_batches_on_overflow(
    user_id: str,
    coordinator: AgentCoordinator,
    agent_factory: UserAgentFactory,
    queue: Any,  # ConsolidationQueue — typed as Any to avoid direct port import in handlers/
    max_batches: Optional[int] = None,
    indexed_email_repo: Any = None,
    user_repo: Any = None,
) -> bool:
    """
    Backward-compatible shim. Delegates to ConsolidationService.process_user_batches.

    Composition layer callers should migrate to passing a ConsolidationService instance
    and calling process_user_batches() directly.
    """
    service = ConsolidationService(
        queue=queue,
        coordinator=coordinator,
        agent_factory=agent_factory,
        indexed_email_repo=indexed_email_repo,
        user_repo=user_repo,
    )
    return await service.process_user_batches(user_id=user_id, max_batches=max_batches)


async def _execute_consolidation_background(
    coordinator: AgentCoordinator,
    agent_factory: UserAgentFactory,
    user_id: str,
    indexed_email_repo: Any = None,
    user_repo: Any = None,
) -> None:
    """
    Background task for consolidation execution.
    Runs without blocking the user's command response.
    """
    from ..domain.agent import AgentMessage, AgentIntent, AgentStatus
    from ..infrastructure.agent_manifest import Intent
    from ..domain.request_context import RequestContext

    try:
        logger.info("👨‍🏫 [Librarian] Background consolidation started for user %s...", user_id)

        await agent_factory.ensure_agents_for_user(user_id)

        user_profile = await user_repo.get_user(user_id) if user_repo else None
        account_id = user_profile.account_id if user_profile else user_id

        async with RequestContext(user_id=user_id, account_id=account_id):
            logger.debug(
                "✅ RequestContext set: user_id=%s, account_id=%s",
                user_id[:8], (account_id[:12] if account_id else "None"),
            )

            message = AgentMessage.create(
                sender="consolidation_handler",
                recipient=f"consolidation_agent_{user_id}",
                intent=AgentIntent.DELEGATE,
                payload={"task": Intent.CONSOLIDATE_FULL},
                context={"user_id": user_id},
            )

            response = await coordinator.route_message(message)

            if response.status != AgentStatus.SUCCESS:
                logger.error("❌ [Librarian] Background consolidation failed: %s", response.error)
            else:
                result = response.result or {}
                logger.info(
                    "✅ [Librarian] Background consolidation completed: stage1_ops=%d, email_batches=%d",
                    result.get("stage1_operations", 0),
                    result.get("email_batches", 0),
                )

    except Exception as exc:
        logger.error("❌ [Librarian] Background consolidation error: %s", exc, exc_info=True)

"""
TaskDispatchService
===================

Thin service wrapper around the TaskQueue port. Provides named dispatch
methods so handlers and other services never import TaskQueue directly.

Usage: inject TaskDispatchService into handlers/services; they call
named methods without knowing the underlying queue implementation.
"""
from __future__ import annotations

from typing import Any, Dict

from ..ports.task_queue import TaskQueue


class TaskDispatchService:
    """Delegates all enqueue operations to the injected TaskQueue port."""

    def __init__(self, task_queue: TaskQueue) -> None:
        self._queue = task_queue

    async def enqueue_agent_task(
        self,
        agent_id: str,
        intent: str,
        query: str,
        context: Dict[str, Any],
        deadline_seconds: int | None = None,
    ) -> str:
        return await self._queue.enqueue_agent_task(
            agent_id=agent_id,
            intent=intent,
            query=query,
            context=context,
            deadline_seconds=deadline_seconds,
        )

    async def enqueue_email_indexing_task(self, job_id: str) -> str:
        return await self._queue.enqueue_email_indexing_task(job_id)

    async def enqueue_consolidation_task(self, user_id: str) -> str:
        return await self._queue.enqueue_consolidation_task(user_id=user_id)

    async def enqueue_deep_research_polling(
        self,
        interaction_id: str,
        user_id: str,
        account_id: str,
        query: str = "",
        attempt: int = 0,
        consecutive_errors: int = 0,
        delay_seconds: int = 30,
        provider: str = "gemini",
        session_id: str = "",
    ) -> str:
        return await self._queue.enqueue_deep_research_polling(
            interaction_id=interaction_id,
            user_id=user_id,
            account_id=account_id,
            query=query,
            attempt=attempt,
            consecutive_errors=consecutive_errors,
            delay_seconds=delay_seconds,
            provider=provider,
            session_id=session_id,
        )

    async def enqueue_worker_task(
        self,
        task_type: str,
        payload: Dict[str, Any],
        delay_seconds: int = 0,
    ) -> str:
        return await self._queue.enqueue_worker_task(
            task_type=task_type,
            payload=payload,
            delay_seconds=delay_seconds,
        )

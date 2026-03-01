"""
Task Queue Port
===============

Defines the interface for async task queues used by adapters (e.g., Cloud Tasks).
"""
from typing import Protocol, Dict, Any, Optional


class TaskQueue(Protocol):
    """Port for enqueueing background tasks."""

    async def enqueue_slack_event(
        self,
        event_data: Dict[str, Any],
        session_id: str,
        delay_seconds: int = 0,
        trace_headers: Optional[Dict[str, str]] = None
    ) -> str:
        """Enqueue a Slack event for background processing."""
        ...

    async def create_queue_if_not_exists(self) -> None:
        """Create the queue if missing."""
        ...

    def get_queue_stats(self) -> Dict[str, Any]:
        """Return queue stats."""
        ...

    async def purge_queue(self) -> None:
        """Purge all queued tasks."""
        ...

    async def enqueue_agent_task(
        self,
        agent_id: str,
        intent: str,
        query: str,
        context: Dict[str, Any]
    ) -> str:
        """
        Enqueue an async agent task for background execution.

        Returns task name (Cloud Tasks task ID).
        Worker receives payload with task_type="agent_execution".
        """
        ...

    async def enqueue_consolidation_task(self, user_id: str) -> str:
        """
        Enqueue a consolidation task for a user.

        Returns task name (Cloud Tasks task ID).
        Worker receives payload with task_type="consolidation".
        """
        ...

    async def enqueue_email_indexing_task(self, job_id: str) -> str:
        """
        Enqueue one email indexing page for a running job.

        Returns task name (Cloud Tasks task ID).
        Worker receives payload with task_type="email_indexing" + job_id.
        One Cloud Tasks request = one Gmail page (~300 emails) = full CPU allocation.
        Worker re-enqueues if job.next_page_token is set after processing.
        """
        ...
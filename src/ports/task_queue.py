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
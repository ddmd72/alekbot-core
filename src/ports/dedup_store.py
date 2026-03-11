"""
DedupStore port — deduplication store for platform events.

Prevents duplicate processing of platform events (Slack, Telegram, etc.)
by tracking processed event/update IDs with a TTL.
"""
from abc import ABC, abstractmethod
from typing import Optional


class DedupStore(ABC):
    """Abstract port for event deduplication."""

    @abstractmethod
    async def is_duplicate(self, event_id: Optional[str]) -> bool:
        """Return True if event_id has already been processed."""

    @abstractmethod
    async def try_mark_processed(self, event_id: str) -> bool:
        """Atomically mark event as processed. Return True if newly marked, False if duplicate."""

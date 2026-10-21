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
        """Atomically claim the event.

        True only when this caller now holds a live mark; False when a live mark exists.
        An expired mark is re-claimable. Errors other than "already marked" raise.
        Residual: the SDK retries a commit on ServiceUnavailable/ResourceExhausted; if the first write landed
        but its response was lost, the retry sees our own mark and returns False (a false duplicate).
        Fixing that needs a claim token.
        """

    @abstractmethod
    async def release(self, event_id: str) -> None:
        """Remove a mark so the event can be processed again (its attempt failed)."""

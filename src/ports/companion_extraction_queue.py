"""
CompanionExtractionQueue — session-scoped analog of ConsolidationQueue
(src/ports/consolidation_queue.py). Every method that ConsolidationQueue
keys by user_id is keyed by session_id here instead — same shape,
different identity axis (RFC §2, §6).

No cleanup_old_batches: ConsolidationQueue's version has no caller in
ConsolidationService either — not proven necessary, so not copied (YAGNI).
"""
from abc import ABC, abstractmethod
from typing import List, Optional

from src.domain.companion_extraction import CompanionExtractionBatch
from src.domain.consolidation import BatchStatus


class CompanionExtractionQueue(ABC):

    @abstractmethod
    async def enqueue_batch(self, batch: CompanionExtractionBatch) -> str:
        """Add a batch to the queue. Returns batch_id."""

    @abstractmethod
    async def get_pending_batches(
        self, session_id: Optional[str] = None, limit: int = 10
    ) -> List[CompanionExtractionBatch]:
        """Get pending/retry_pending batches, optionally filtered by session_id."""

    @abstractmethod
    async def get_queue_size(self, session_id: str) -> int:
        """Count total messages in ALL batches for this session."""

    @abstractmethod
    async def delete_batch(self, batch_id: str) -> None:
        """Delete batch after successful processing."""

    @abstractmethod
    async def update_batch_status(
        self,
        batch_id: str,
        status: BatchStatus,
        error: Optional[str] = None,
        records_extracted: int = 0,
    ) -> None:
        """Update batch status atomically."""

    @abstractmethod
    async def increment_attempts(self, batch_id: str) -> int:
        """Increment attempt counter. Returns new count."""

    @abstractmethod
    async def reset_recoverable_batches(self, session_id: str) -> int:
        """Reset stale PROCESSING (zombies) + FAILED batches -> RETRY_PENDING for this session."""

    @abstractmethod
    async def get_stuck_session_ids(self) -> List[str]:
        """Return distinct session_ids with at least one batch still in the queue."""

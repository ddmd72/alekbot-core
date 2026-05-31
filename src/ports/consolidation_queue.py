from abc import ABC, abstractmethod
from typing import List, Optional
from src.domain.consolidation import ConsolidationBatch, BatchStatus

class ConsolidationQueue(ABC):
    """
    Abstract Port for consolidation batch queue management.
    """

    @abstractmethod
    async def enqueue_batch(self, batch: ConsolidationBatch) -> str:
        """Add a batch to the consolidation queue. Returns batch_id."""
        pass

    @abstractmethod
    async def get_pending_batches(self, user_id: Optional[str] = None, limit: int = 10) -> List[ConsolidationBatch]:
        """Get pending/retry_pending batches, optionally filtered by user_id."""
        pass

    @abstractmethod
    async def get_queue_size(self, user_id: str) -> int:
        """Count total messages in ALL batches for user."""
        pass

    @abstractmethod
    async def delete_batch(self, batch_id: str) -> None:
        """Delete batch after successful processing."""
        pass

    @abstractmethod
    async def cleanup_old_batches(self, user_id: str, max_messages: int = 600) -> int:
        """Delete oldest completed/failed batches if queue > max. Returns deleted count."""
        pass

    @abstractmethod
    async def update_batch_status(
        self,
        batch_id: str,
        status: BatchStatus,
        error: Optional[str] = None,
        facts_extracted: int = 0
    ) -> None:
        """Update batch status atomically."""
        pass

    @abstractmethod
    async def increment_attempts(self, batch_id: str) -> int:
        """Increment attempt counter. Returns new count."""
        pass

    @abstractmethod
    async def reset_recoverable_batches(self, user_id: str) -> int:
        """Reset stale PROCESSING (zombies) + FAILED batches → RETRY_PENDING for this user.

        Called at the start of each consolidation run. Two recovery paths:
          - PROCESSING zombies: workers crashed / Cloud Run CPU-throttled mid-batch.
            Only batches whose `processing_started_at` is older than the consolidation
            Cloud Task deadline are reset — a recent PROCESSING batch is a LIVE run and
            must be left alone (otherwise the periodic sweep would race a running
            consolidation and double-process the batch).
          - FAILED batches: marked failed after 3 attempts on prior runs. Most failures
            are transient (LLM 5xx, rate limits, billing exhaustion); user prefers
            automatic retry to avoid silent data loss. Periodic Firestore cron purges
            truly stuck FAILED rows by age (manual external cleanup).

        Resets `attempts` to 0 and clears `last_error` so the retry starts clean.
        Returns count of batches reset."""
        pass

    @abstractmethod
    async def get_stuck_batch_user_ids(self) -> List[str]:
        """Return distinct user_ids that have at least one batch still in the queue.

        Batches are deleted on successful consolidation (delete_batch), so any stored
        batch is unconsolidated work — its data has been extracted from session history
        but not yet written to memory. The hourly sweep scheduler uses this to re-trigger
        consolidation for affected users instead of waiting for the next overflow."""
        pass

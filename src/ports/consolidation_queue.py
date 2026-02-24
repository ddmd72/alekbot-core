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
    async def reset_processing_batches(self, user_id: str) -> int:
        """Reset stale PROCESSING batches → RETRY_PENDING for this user.
        Used at the start of each consolidation run to recover zombies left
        by crashed or CPU-throttled workers. Returns count of batches reset."""
        pass

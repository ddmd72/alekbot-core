"""
CompanionExtractionQueue — session-scoped analog of ConsolidationQueue
(src/ports/consolidation_queue.py). Every method that ConsolidationQueue
keys by user_id is keyed by session_id here instead — same shape,
different identity axis (RFC §2, §6).

No cleanup_old_batches: it had no caller and was removed from ConsolidationQueue too.
"""
from abc import ABC, abstractmethod
from typing import List, Optional

from src.domain.companion_extraction import CompanionExtractionBatch
from src.domain.consolidation import BatchStatus


class CompanionExtractionQueue(ABC):
    """Port for the per-session queue of companion extraction batches (one record per batch, id `batch_id`).

    Holds for every method:
    - Tenancy: reads take a session_id (user_id:channel_id or platform:channel_id) and return only that session's
      batches; account_id travels in the batch; writes by batch_id (a random id) are unscoped.
    - Atomicity: no claim and no lease: two concurrent readers process the same batch (P15-D5).
    - Legacy: a stored batch is read as a CompanionExtractionBatch (unknown fields ignored). A batch missing a required
      field or `created_at`: no writer produces one; behaviour pending the data audit.
    """

    @abstractmethod
    async def enqueue_batch(self, batch: CompanionExtractionBatch) -> str:
        """Store the batch under its batch_id (a replace). Returns batch_id.

        Idempotency: the same batch twice is one record."""

    @abstractmethod
    async def get_pending_batches(
        self, session_id: str, limit: int = 10
    ) -> List[CompanionExtractionBatch]:
        """PENDING and RETRY_PENDING batches of session_id, oldest created_at first, at most `limit`.

        Errors: backend and validation errors propagate."""

    @abstractmethod
    async def get_queue_size(self, session_id: str) -> int:
        """Total messages over all of this session's batches. No caller in src/."""

    @abstractmethod
    async def delete_batch(self, batch_id: str) -> None:
        """Delete the batch after successful processing; deleting a missing batch is a no-op."""

    @abstractmethod
    async def update_batch_status(
        self,
        batch_id: str,
        status: BatchStatus,
        error: Optional[str] = None,
        records_extracted: int = 0,
    ) -> None:
        """Set status and records_extracted (0 unless given); last_error only when `error` is given; PROCESSING also
        stamps processing_started_at (now).

        Errors: the backend's not-found error (NotFound today) on a missing batch."""

    @abstractmethod
    async def increment_attempts(self, batch_id: str) -> int:
        """attempts + 1, returned; 0 (and no write) for a missing batch. Read then write, no transaction."""

    @abstractmethod
    async def reset_recoverable_batches(self, session_id: str) -> int:
        """Reset this session's zombies to RETRY_PENDING (attempts 0, last_error and processing_started_at cleared):
        PROCESSING batches older than the task deadline (1800 s) or without processing_started_at; a younger
        PROCESSING batch is a live run and is left alone. A FAILED batch is revived a bounded number of times.
        (P15-D5: today every FAILED batch is reset on every call with no counter, and the per-task path calls this,
        so a batch that always fails is extracted without bound.)
        Returns the number of batches reset."""

    @abstractmethod
    async def get_stuck_session_ids(self) -> List[str]:
        """Distinct session_ids with at least one batch still in the queue (any status)."""

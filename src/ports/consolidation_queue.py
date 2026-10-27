from abc import ABC, abstractmethod
from typing import List, Optional
from src.domain.consolidation import ConsolidationBatch, BatchStatus

class ConsolidationQueue(ABC):
    """Port for the per-user queue of consolidation batches (one record per batch, id `batch_id`).

    Holds for every method:
    - Tenancy: reads take a user_id and return only that user's batches; writes by batch_id (a random id) are unscoped.
    - Atomicity: no method claims a batch; two concurrent readers get the same batch. Runs of one account are
      serialised by the memory-write lease in ConsolidationService, not here.
    - Legacy: a stored batch is read as a ConsolidationBatch (unknown fields ignored). A batch missing a required field
      or `created_at`: no writer produces one; behaviour pending the data audit.
    """

    @abstractmethod
    async def enqueue_batch(self, batch: ConsolidationBatch) -> str:
        """Store the batch under its batch_id (a replace). Returns batch_id.

        Idempotency: the same batch twice is one record."""
        pass

    @abstractmethod
    async def get_pending_batches(self, user_id: str, limit: int = 10) -> List[ConsolidationBatch]:
        """PENDING and RETRY_PENDING batches of user_id, oldest created_at first, at most `limit`.

        Errors: backend and validation errors propagate."""
        pass

    @abstractmethod
    async def get_queue_size(self, user_id: str) -> int:
        """Total messages over all of the user's batches. No caller in src/."""
        pass

    @abstractmethod
    async def delete_batch(self, batch_id: str) -> None:
        """Delete the batch after successful processing; deleting a missing batch is a no-op."""
        pass

    @abstractmethod
    async def update_batch_status(
        self,
        batch_id: str,
        status: BatchStatus,
        error: Optional[str] = None,
        facts_extracted: int = 0
    ) -> None:
        """Set status and facts_extracted (0 unless given); last_error only when `error` is given; PROCESSING also
        stamps processing_started_at (now).

        Errors: the backend's not-found error (NotFound today) on a missing batch."""
        pass

    @abstractmethod
    async def increment_attempts(self, batch_id: str) -> int:
        """attempts + 1, returned; 0 (and no write) for a missing batch.

        Atomicity: read then write, no transaction; callers hold the account lease."""
        pass

    @abstractmethod
    async def reset_recoverable_batches(
        self, user_id: str, revive_failed: bool = False, zombie_after_s: Optional[float] = None
    ) -> int:
        """Reset stale PROCESSING (zombies) → RETRY_PENDING for this user; with
        `revive_failed=True` also FAILED batches that still have revivals left.

        `zombie_after_s` is the age past which a PROCESSING batch counts as a zombie; `None` takes the
        adapter's default (the consolidation Cloud Task deadline). A caller holding the account's
        memory-write lease passes the lease margin: every earlier run has then ended or expired.

        Called at the start of each consolidation run (zombies only) and by the hourly
        sweep (`revive_failed=True`).
          - PROCESSING zombies: workers crashed / Cloud Run CPU-throttled mid-batch.
            Only batches whose `processing_started_at` is older than the consolidation
            Cloud Task deadline are reset — a recent PROCESSING batch is a LIVE run and
            must be left alone (otherwise the periodic sweep would race a running
            consolidation and double-process the batch).
          - FAILED batches (only with `revive_failed=True`): marked failed after 3 attempts.
            Most failures are transient (LLM 5xx, rate limits, billing exhaustion), so the
            sweep retries them — but each batch is revived at most MAX_FAILED_REVIVALS times
            in its life (`revivals`, never reset); past that it stays FAILED with its messages.
            The per-task path must not revive FAILED: it re-enqueues itself back to back.

        A zombie reset counts the lost run (`attempts` + 1) and fails the batch at MAX_BATCH_ATTEMPTS; a FAILED revival resets
        `attempts` to 0. Clears `last_error`; `revivals` is kept (+1 for a FAILED revival).
        Legacy: a PROCESSING batch without processing_started_at is a zombie; a FAILED batch without revivals has 0.
        Atomicity: one write per batch, no precondition.
        Returns count of batches reset."""
        pass

    @abstractmethod
    async def has_open_batches(self, user_id: str) -> bool:
        """True when the user has a batch that is pending, retry_pending or processing: work that is queued
        or in flight and so still needs a consolidation run. FAILED batches do not count. Tenancy: this user only."""
        pass

    @abstractmethod
    async def get_stuck_batch_user_ids(self) -> List[str]:
        """Return distinct user_ids that have at least one batch still in the queue.

        Batches are deleted on successful consolidation (delete_batch), so any stored
        batch is unconsolidated work — its data has been extracted from session history
        but not yet written to memory. The hourly sweep scheduler uses this to re-trigger
        consolidation for affected users instead of waiting for the next overflow. Includes users whose only batch
        is FAILED past MAX_FAILED_REVIVALS (the sweep then enqueues a run that finds nothing)."""
        pass

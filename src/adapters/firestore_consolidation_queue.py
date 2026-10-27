import logging
import time
from typing import Any, Dict, List, Optional
from google.cloud import firestore
from google.cloud.firestore import FieldFilter
from src.ports.consolidation_queue import ConsolidationQueue
from src.domain.consolidation import ConsolidationBatch, BatchStatus, MAX_BATCH_ATTEMPTS, MAX_FAILED_REVIVALS
from src.config.environment import EnvironmentConfig

logger = logging.getLogger(__name__)

# A batch can only be legitimately PROCESSING while its consolidation Cloud Task is
# alive. That task's dispatch_deadline is 1800s (see gcp_task_queue.enqueue_consolidation_task),
# and ConsolidationAgent's own timeout is 15 min — both bounded by 1800s. A PROCESSING
# batch older than this can only be a zombie (worker crashed / CPU-throttled mid-batch),
# so it is safe to reset. Anything younger is a live run and must be left alone.
_ZOMBIE_THRESHOLD_SECONDS = 1800


class FirestoreConsolidationQueue(ConsolidationQueue):
    """
    Firestore implementation of the ConsolidationQueue port.

    Firestore mechanics (the port states the observable contract): collection
    `EnvironmentConfig.consolidation_queue_collection` (`{prefix}consolidation_queue`), document id = `batch_id`, body
    `ConsolidationBatch.model_dump()` written with a plain `set`. `get_pending_batches` needs the composite index
    (user_id, status, created_at); a batch without `created_at` is therefore never returned by it, while
    `has_open_batches` and `get_stuck_batch_user_ids` (a `select(["user_id"])` projection) still see it.
    `increment_attempts` is a get then an update; `reset_recoverable_batches` one update per batch, no precondition.
    """
    def __init__(self, db_client: firestore.AsyncClient, env_config: EnvironmentConfig):
        self.db = db_client
        self.env_config = env_config
        # ADR-006: Use semantic collection name
        self.collection_name = env_config.consolidation_queue_collection
        self.collection = self.db.collection(self.collection_name)
        logger.info(f"📂 FirestoreConsolidationQueue initialized with collection: {self.collection_name}")
    
    async def enqueue_batch(self, batch: ConsolidationBatch) -> str:
        doc_ref = self.collection.document(batch.batch_id)
        await doc_ref.set(batch.model_dump())
        return batch.batch_id
    
    async def get_pending_batches(self, user_id: str, limit: int = 10) -> List[ConsolidationBatch]:
        query = self.collection.where(
            filter=FieldFilter("status", "in", [BatchStatus.PENDING.value, BatchStatus.RETRY_PENDING.value])
        )
        query = query.where(filter=FieldFilter("user_id", "==", user_id))
            
        # Oldest first; needs the (user_id, status, created_at) composite index.
        query = query.order_by("created_at").limit(limit)
        
        docs = await query.get()
        return [ConsolidationBatch(**doc.to_dict()) for doc in docs]
    
    async def update_batch_status(
        self, 
        batch_id: str, 
        status: BatchStatus,
        error: Optional[str] = None,
        facts_extracted: int = 0
    ) -> None:
        doc_ref = self.collection.document(batch_id)
        update_data: Dict[str, Any] = {
            "status": status.value,
            "facts_extracted": facts_extracted
        }
        if error:
            update_data["last_error"] = error
        # Stamp entry into PROCESSING so reset_recoverable_batches can tell a live run
        # from a zombie. Cleared again when the batch is reset back to RETRY_PENDING.
        if status == BatchStatus.PROCESSING:
            update_data["processing_started_at"] = time.time()

        await doc_ref.update(update_data)
    
    async def increment_attempts(self, batch_id: str) -> int:
        doc_ref = self.collection.document(batch_id)
        # Using transactional increment if possible or standard update
        # For simplicity and given the usage pattern, a standard update is usually fine
        doc = await doc_ref.get()
        if not doc.exists:
            return 0
        
        data = doc.to_dict()
        new_attempts = data.get("attempts", 0) + 1
        await doc_ref.update({"attempts": new_attempts})
        return new_attempts
    
    async def get_queue_size(self, user_id: str) -> int:
        """Count total messages in ALL batches for user."""
        query = self.collection.where(filter=FieldFilter("user_id", "==", user_id))
        docs = await query.get()
        
        total = 0
        for doc in docs:
            data = doc.to_dict()
            total += len(data.get("messages", []))
        return total

    async def delete_batch(self, batch_id: str) -> None:
        """Delete batch document from Firestore."""
        doc_ref = self.collection.document(batch_id)
        await doc_ref.delete()
        logger.debug(f"🗑️ Deleted batch {batch_id}")
    
    async def has_open_batches(self, user_id: str) -> bool:
        query = (
            self.collection
            .where(filter=FieldFilter("status", "in", [
                BatchStatus.PENDING.value, BatchStatus.RETRY_PENDING.value, BatchStatus.PROCESSING.value,
            ]))
            .where(filter=FieldFilter("user_id", "==", user_id))
            .limit(1)
        )
        return len(await query.get()) > 0

    async def reset_recoverable_batches(
        self, user_id: str, revive_failed: bool = False, zombie_after_s: Optional[float] = None
    ) -> int:
        """Reset stale PROCESSING (zombies) → RETRY_PENDING; with `revive_failed` also FAILED.

        PROCESSING batches are reset only when their `processing_started_at` is older than
        `zombie_after_s` (default _ZOMBIE_THRESHOLD_SECONDS) — a recent PROCESSING batch is a LIVE run
        (its Cloud Task is still executing) and must be left untouched, otherwise the hourly sweep would
        reset a running consolidation and double-process the batch.

        FAILED batches are revived only when `revive_failed` is set (the hourly sweep) and the
        batch has `revivals < MAX_FAILED_REVIVALS`; each revival adds 1 to `revivals`, which no
        reset clears. A FAILED batch past the cap is left as it is.

        A zombie reset counts the lost run: `attempts` + 1. A run that kills its worker never records a failure
        itself, so without this count such a batch would loop for ever; at MAX_BATCH_ATTEMPTS it goes FAILED
        (the bounded sweep revivals then apply). A FAILED revival resets `attempts` to 0.

        Clears `last_error` + `processing_started_at`. Returns the number of batches actually reset.
        """
        threshold = _ZOMBIE_THRESHOLD_SECONDS if zombie_after_s is None else zombie_after_s
        statuses = [BatchStatus.PROCESSING.value]
        if revive_failed:
            statuses.append(BatchStatus.FAILED.value)
        query = (
            self.collection
            .where(filter=FieldFilter("status", "in", statuses))
            .where(filter=FieldFilter("user_id", "==", user_id))
        )
        docs = await query.get()
        now = time.time()
        zombies = 0
        failures = 0
        live = 0
        exhausted = 0
        for doc in docs:
            data = doc.to_dict() or {}
            prev = data.get("status")
            given_up = 0
            update: Dict[str, Any] = {
                "status": BatchStatus.RETRY_PENDING.value,
                "attempts": 0,
                "last_error": None,
                "processing_started_at": None,
            }
            if prev == BatchStatus.PROCESSING.value:
                started = data.get("processing_started_at")
                # Missing timestamp (legacy batch) is treated as a zombie — safe, since
                # any pre-existing PROCESSING batch predates this stamping logic.
                if started is not None and (now - started) < threshold:
                    live += 1
                    continue
                lost_runs = data.get("attempts", 0) + 1
                update["attempts"] = lost_runs
                if lost_runs >= MAX_BATCH_ATTEMPTS:
                    update["status"] = BatchStatus.FAILED.value
                    update["last_error"] = f"run lost while processing (worker crash or timeout), {lost_runs} times"
                    given_up = lost_runs
            else:
                revivals = data.get("revivals", 0)
                if revivals >= MAX_FAILED_REVIVALS:
                    exhausted += 1
                    continue
                update["revivals"] = revivals + 1
            await doc.reference.update(update)
            if given_up:
                self._log_given_up_zombie(doc.id, data, given_up)
            if prev == BatchStatus.PROCESSING.value:
                zombies += 1
            else:
                failures += 1
        if zombies:
            logger.info(
                f"♻️ Reset {zombies} stale PROCESSING batches (attempts +1) "
                f"for user {user_id[:8]}"
            )
        if failures:
            logger.info(
                f"♻️ Revived {failures} FAILED batches → RETRY_PENDING (attempts=0) "
                f"for user {user_id[:8]}"
            )
        if live:
            logger.debug(
                f"⏳ Skipped {live} live PROCESSING batches (< {threshold}s) "
                f"for user {user_id[:8]}"
            )
        if exhausted:
            logger.debug(
                f"⏹️ Left {exhausted} FAILED batches (revivals >= {MAX_FAILED_REVIVALS}) "
                f"for user {user_id[:8]}"
            )
        return zombies + failures

    @staticmethod
    def _log_given_up_zombie(batch_id: str, data: Dict[str, Any], lost_runs: int) -> None:
        # Same grep targets as the service's own failure lines.
        user = str(data.get("user_id", ""))
        messages = len(data.get("messages", []))
        logger.error(
            "❌ [Librarian] CONSOLIDATION_FAILED batch_id=%s user_id=%s attempts=%d messages=%d "
            "error='run lost while processing'", batch_id, user, lost_runs, messages,
        )
        if data.get("revivals", 0) >= MAX_FAILED_REVIVALS:
            logger.error(
                "❌ [Librarian] CONSOLIDATION_GAVE_UP batch_id=%s user_id=%s revivals=%d messages=%d "
                "(kept in the queue, not revived again)", batch_id, user[:8], data.get("revivals", 0), messages,
            )

    async def get_stuck_batch_user_ids(self) -> List[str]:
        """Distinct user_ids with at least one batch still in the queue.

        Successful batches are deleted, so every stored batch is unconsolidated work.
        Uses a field projection (`user_id` only) to keep the read cheap regardless of
        batch message payload size.
        """
        query = self.collection.select(["user_id"])
        docs = await query.get()
        user_ids = set()
        for doc in docs:
            data = doc.to_dict() or {}
            uid = data.get("user_id")
            if uid:
                user_ids.add(uid)
        return list(user_ids)

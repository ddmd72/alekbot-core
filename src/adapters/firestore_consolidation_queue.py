import logging
import time
from typing import Any, Dict, List, Optional
from google.cloud import firestore
from google.cloud.firestore import FieldFilter
from src.ports.consolidation_queue import ConsolidationQueue
from src.domain.consolidation import ConsolidationBatch, BatchStatus, MAX_FAILED_REVIVALS
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

        Resets `attempts` to 0 and clears `last_error` + `processing_started_at`.
        Returns the number of batches actually reset.
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
            else:
                revivals = data.get("revivals", 0)
                if revivals >= MAX_FAILED_REVIVALS:
                    exhausted += 1
                    continue
                update["revivals"] = revivals + 1
            await doc.reference.update(update)
            if prev == BatchStatus.PROCESSING.value:
                zombies += 1
            else:
                failures += 1
        if zombies:
            logger.info(
                f"♻️ Reset {zombies} stale PROCESSING batches → RETRY_PENDING "
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

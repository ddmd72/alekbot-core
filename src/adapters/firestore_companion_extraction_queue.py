"""
FirestoreCompanionExtractionQueue — Firestore implementation of
CompanionExtractionQueue. Structurally identical to
FirestoreConsolidationQueue (src/adapters/firestore_consolidation_queue.py),
session_id in place of user_id throughout.
"""
import logging
import time
from typing import Any, Dict, List, Optional
from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore
from google.cloud.firestore import FieldFilter

from src.ports.companion_extraction_queue import CompanionExtractionQueue
from src.domain.companion_extraction import CompanionExtractionBatch
from src.domain.consolidation import MAX_FAILED_REVIVALS, BatchStatus
from src.config.environment import EnvironmentConfig

logger = logging.getLogger(__name__)

# A batch can only legitimately be PROCESSING while its companion_consolidation
# Cloud Task is alive. TutorExtractorAgent's timeout (TUTOR_EXTRACTOR.timeout_ms,
# see src/infrastructure/agent_config.py) is 5 min; the enqueue dispatch_deadline
# (Task 9) is 600s — both well under this threshold.
_ZOMBIE_THRESHOLD_SECONDS = 1800


class FirestoreCompanionExtractionQueue(CompanionExtractionQueue):
    """Firestore mechanics (the port states the observable contract): collection
    `EnvironmentConfig.companion_extraction_queue_collection`, document id = `batch_id` (`cbatch_<24 hex>` from the
    overflow, `cbatch_<12 hex>` by default), body `CompanionExtractionBatch.model_dump()` written with `create` (an
    existing id raises AlreadyExists, taken as success). `get_pending_batches` needs the composite index
    (session_id, status, created_at); `get_stuck_session_ids` is a `select(["session_id"])` projection;
    `increment_attempts` is a get then an update; `reset_recoverable_batches` one update per batch, no precondition; a FAILED batch is revived at most
    `MAX_FAILED_REVIVALS` times (the `revivals` field, never reset)."""

    def __init__(self, db_client: firestore.AsyncClient, env_config: EnvironmentConfig):
        self.db = db_client
        self.env_config = env_config
        self.collection_name = env_config.companion_extraction_queue_collection
        self.collection = self.db.collection(self.collection_name)
        logger.info(f"📂 FirestoreCompanionExtractionQueue initialized with collection: {self.collection_name}")

    async def enqueue_batch(self, batch: CompanionExtractionBatch) -> str:
        doc_ref = self.collection.document(batch.batch_id)
        try:
            await doc_ref.create(batch.model_dump())
        except AlreadyExists:
            # A repeated hand-off (same deterministic id): the stored batch keeps its status and attempts.
            logger.info(f"📦 Batch {batch.batch_id} already queued; kept as stored")
        return batch.batch_id

    async def get_pending_batches(
        self, session_id: str, limit: int = 10
    ) -> List[CompanionExtractionBatch]:
        query = self.collection.where(
            filter=FieldFilter("status", "in", [BatchStatus.PENDING.value, BatchStatus.RETRY_PENDING.value])
        )
        query = query.where(filter=FieldFilter("session_id", "==", session_id))
        # Oldest first; needs the (session_id, status, created_at) composite index.
        query = query.order_by("created_at").limit(limit)
        docs = await query.get()
        return [CompanionExtractionBatch(**doc.to_dict()) for doc in docs]

    async def update_batch_status(
        self,
        batch_id: str,
        status: BatchStatus,
        error: Optional[str] = None,
        records_extracted: int = 0,
    ) -> None:
        doc_ref = self.collection.document(batch_id)
        update_data: Dict[str, Any] = {"status": status.value, "records_extracted": records_extracted}
        if error:
            update_data["last_error"] = error
        if status == BatchStatus.PROCESSING:
            update_data["processing_started_at"] = time.time()
        await doc_ref.update(update_data)

    async def increment_attempts(self, batch_id: str) -> int:
        doc_ref = self.collection.document(batch_id)
        doc = await doc_ref.get()
        if not doc.exists:
            return 0
        data = doc.to_dict()
        new_attempts = data.get("attempts", 0) + 1
        await doc_ref.update({"attempts": new_attempts})
        return new_attempts

    async def get_queue_size(self, session_id: str) -> int:
        query = self.collection.where(filter=FieldFilter("session_id", "==", session_id))
        docs = await query.get()
        return sum(len(doc.to_dict().get("messages", [])) for doc in docs)

    async def delete_batch(self, batch_id: str) -> None:
        await self.collection.document(batch_id).delete()
        logger.debug(f"🗑️ Deleted batch {batch_id}")

    async def reset_recoverable_batches(self, session_id: str) -> int:
        query = (
            self.collection
            .where(filter=FieldFilter("status", "in", [BatchStatus.PROCESSING.value, BatchStatus.FAILED.value]))
            .where(filter=FieldFilter("session_id", "==", session_id))
        )
        docs = await query.get()
        now = time.time()
        reset = 0
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
                if started is not None and (now - started) < _ZOMBIE_THRESHOLD_SECONDS:
                    continue
            else:
                revivals = data.get("revivals", 0)
                if revivals >= MAX_FAILED_REVIVALS:
                    exhausted += 1
                    continue
                update["revivals"] = revivals + 1
            await doc.reference.update(update)
            reset += 1
        if exhausted:
            logger.debug(
                f"⏹️ Left {exhausted} FAILED batches (revivals >= {MAX_FAILED_REVIVALS}) "
                f"for session {session_id[:12]}"
            )
        return reset

    async def get_stuck_session_ids(self) -> List[str]:
        query = self.collection.select(["session_id"])
        docs = await query.get()
        session_ids = set()
        for doc in docs:
            sid = (doc.to_dict() or {}).get("session_id")
            if sid:
                session_ids.add(sid)
        return list(session_ids)

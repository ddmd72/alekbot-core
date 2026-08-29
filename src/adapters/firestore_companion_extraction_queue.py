"""
FirestoreCompanionExtractionQueue — Firestore implementation of
CompanionExtractionQueue. Structurally identical to
FirestoreConsolidationQueue (src/adapters/firestore_consolidation_queue.py),
session_id in place of user_id throughout.
"""
import logging
import time
from typing import List, Optional
from google.cloud import firestore
from google.cloud.firestore import FieldFilter

from src.ports.companion_extraction_queue import CompanionExtractionQueue
from src.domain.companion_extraction import CompanionExtractionBatch
from src.domain.consolidation import BatchStatus
from src.config.environment import EnvironmentConfig

logger = logging.getLogger(__name__)

# A batch can only legitimately be PROCESSING while its companion_consolidation
# Cloud Task is alive. TutorExtractorAgent's timeout (TUTOR_EXTRACTOR.timeout_ms,
# see src/infrastructure/agent_config.py) is 5 min; the enqueue dispatch_deadline
# (Task 9) is 600s — both well under this threshold.
_ZOMBIE_THRESHOLD_SECONDS = 1800


class FirestoreCompanionExtractionQueue(CompanionExtractionQueue):

    def __init__(self, db_client: firestore.AsyncClient, env_config: EnvironmentConfig):
        self.db = db_client
        self.env_config = env_config
        self.collection_name = env_config.companion_extraction_queue_collection
        self.collection = self.db.collection(self.collection_name)
        logger.info(f"📂 FirestoreCompanionExtractionQueue initialized with collection: {self.collection_name}")

    async def enqueue_batch(self, batch: CompanionExtractionBatch) -> str:
        doc_ref = self.collection.document(batch.batch_id)
        await doc_ref.set(batch.model_dump())
        return batch.batch_id

    async def get_pending_batches(
        self, session_id: Optional[str] = None, limit: int = 10
    ) -> List[CompanionExtractionBatch]:
        query = self.collection.where(
            filter=FieldFilter("status", "in", [BatchStatus.PENDING.value, BatchStatus.RETRY_PENDING.value])
        )
        if session_id:
            query = query.where(filter=FieldFilter("session_id", "==", session_id))
        query = query.limit(limit)
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
        update_data = {"status": status.value, "records_extracted": records_extracted}
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
        for doc in docs:
            data = doc.to_dict() or {}
            prev = data.get("status")
            if prev == BatchStatus.PROCESSING.value:
                started = data.get("processing_started_at")
                if started is not None and (now - started) < _ZOMBIE_THRESHOLD_SECONDS:
                    continue
            await doc.reference.update({
                "status": BatchStatus.RETRY_PENDING.value,
                "attempts": 0,
                "last_error": None,
                "processing_started_at": None,
            })
            reset += 1
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

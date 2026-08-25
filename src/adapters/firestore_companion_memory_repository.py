"""
FirestoreCompanionMemoryRepository — Firestore implementation of
CompanionMemoryRepository. One collection, session_id-scoped.

Structurally mirrors FirestoreIndexedEmailRepository (Vector() wrapping,
500-doc batch chunking, RRF-ready find_nearest), not FirestoreFactRepository
— see CompanionMemoryRepository port docstring / RFC §11.
"""
from typing import List

from google.cloud.firestore import FieldFilter
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector

from ..config.environment import EnvironmentConfig
from ..domain.companion import CompanionRecord
from ..ports.companion_memory_repository import CompanionMemoryRepository
from ..utils.logger import logger


class FirestoreCompanionMemoryRepository(CompanionMemoryRepository):

    def __init__(self, db_client, env_config: EnvironmentConfig):
        self.db = db_client
        self.collection = self.db.collection(env_config.companion_records_collection)
        logger.info(
            "🧑‍🏫 CompanionMemoryRepository initialized: %s",
            env_config.companion_records_collection,
        )

    async def save_batch(self, records: List[CompanionRecord]) -> int:
        if not records:
            return 0

        written = 0
        chunk_size = 500
        for i in range(0, len(records), chunk_size):
            chunk = records[i : i + chunk_size]
            batch = self.db.batch()
            for record in chunk:
                data = record.model_dump()
                if data.get("vector") is not None:
                    data["vector"] = Vector(data["vector"])
                doc_ref = self.collection.document(record.id)
                batch.set(doc_ref, data)
                written += 1
            await batch.commit()

        logger.info("💾 [CompanionMemory] save_batch: %d docs written", written)
        return written

    async def find_nearest(
        self,
        session_id: str,
        query_vector: List[float],
        limit: int = 10,
    ) -> List[CompanionRecord]:
        query = (
            self.collection
            .where(filter=FieldFilter("session_id", "==", session_id))
            .find_nearest(
                vector_field="vector",
                query_vector=query_vector,
                distance_measure=DistanceMeasure.COSINE,
                limit=limit,
            )
        )
        docs = await query.get()

        records = []
        for doc in docs:
            data = doc.to_dict()
            vector = data.get("vector")
            if vector is not None and not isinstance(vector, list):
                data["vector"] = list(vector)
            try:
                records.append(CompanionRecord(**data))
            except Exception as exc:
                logger.error(
                    "💥 [CompanionMemory] find_nearest failed to parse %s: %s",
                    doc.id, exc,
                )
        return records

"""
FirestoreTaskSearchIndex — implements TaskSearchIndex via Firestore vector queries.

Collection: {env}_task_search_index (doc ID = {user_id}_{task_id})
Vectors: content_vector, context_vector
RRF across both vectors; filters: user_id, optionally list_id, status != completed.

Mirrors FirestoreIndexedEmailRepository pattern for vector wrap/unwrap and RRF.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Dict, List, Optional

from google.cloud.firestore import FieldFilter
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector

from ..config.environment import EnvironmentConfig
from ..domain.task import TaskImportance, TaskSearchEntry, TaskStatus
from ..ports.task_search_index import TaskSearchIndex
from ..utils.logger import logger

_VECTOR_FIELDS = ("content_vector", "context_vector")
_RRF_K = 60
_MAX_COSINE_DISTANCE = 0.4
_FIND_NEAREST_SEMAPHORE = asyncio.Semaphore(10)


class FirestoreTaskSearchIndex(TaskSearchIndex):

    def __init__(self, db_client, env_config: EnvironmentConfig) -> None:
        self.db = db_client
        col = env_config.task_search_index_collection
        self.collection = self.db.collection(col)
        logger.info(f"📋 FirestoreTaskSearchIndex initialized (collection: {col})")

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _wrap_vectors(data: dict) -> dict:
        for field in _VECTOR_FIELDS:
            if data.get(field) is not None:
                data[field] = Vector(data[field])
        return data

    @staticmethod
    def _unwrap_vectors(data: dict) -> dict:
        for field in _VECTOR_FIELDS:
            v = data.get(field)
            if v is not None and not isinstance(v, list):
                data[field] = list(v)
        return data

    @staticmethod
    def _strip_tz(dt):
        if dt is None:
            return None
        if isinstance(dt, datetime):
            return datetime(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, dt.microsecond)
        return dt

    def _doc_id(self, user_id: str, task_id: str) -> str:
        return f"{user_id}_{task_id}"

    def _to_domain(self, data: dict) -> TaskSearchEntry:
        self._unwrap_vectors(data)
        if "indexed_at" in data:
            data["indexed_at"] = self._strip_tz(data["indexed_at"]) or datetime.utcnow()
        # Coerce enum strings
        if isinstance(data.get("status"), str):
            data["status"] = TaskStatus(data["status"])
        if isinstance(data.get("importance"), str):
            data["importance"] = TaskImportance(data["importance"])
        return TaskSearchEntry(**data)

    # ------------------------------------------------------------------
    # TaskSearchIndex — upsert
    # ------------------------------------------------------------------

    async def upsert(self, entry: TaskSearchEntry) -> None:
        data = entry.model_dump()
        # Serialize enums to their string values
        data["status"] = entry.status.value
        data["importance"] = entry.importance.value
        self._wrap_vectors(data)
        doc_ref = self.collection.document(self._doc_id(entry.user_id, entry.task_id))
        await doc_ref.set(data)
        logger.debug(f"📋 TaskSearchIndex upserted: {entry.task_id[:8]}")

    # ------------------------------------------------------------------
    # TaskSearchIndex — delete
    # ------------------------------------------------------------------

    async def delete(self, user_id: str, task_id: str) -> None:
        doc_ref = self.collection.document(self._doc_id(user_id, task_id))
        await doc_ref.delete()
        logger.debug(f"📋 TaskSearchIndex deleted: {task_id[:8]}")

    # ------------------------------------------------------------------
    # TaskSearchIndex — delete_by_list
    # ------------------------------------------------------------------

    async def delete_by_list(self, user_id: str, list_id: str) -> None:
        """Batch delete all tasks in a list from the index (Firestore 500-write limit)."""
        query = (
            self.collection
            .where(filter=FieldFilter("user_id", "==", user_id))
            .where(filter=FieldFilter("list_id", "==", list_id))
        )
        docs = await query.get()
        chunk_size = 500
        for i in range(0, len(docs), chunk_size):
            batch = self.db.batch()
            for doc in docs[i : i + chunk_size]:
                batch.delete(doc.reference)
            await batch.commit()
        logger.info(f"📋 TaskSearchIndex deleted {len(docs)} docs for list {list_id[:8]}")

    # ------------------------------------------------------------------
    # TaskSearchIndex — find_nearest (multi-vector RRF)
    # ------------------------------------------------------------------

    async def find_nearest(
        self,
        user_id: str,
        vectors: Dict[str, List[float]],
        limit: int = 10,
        show_completed: bool = False,
        list_id: Optional[str] = None,
    ) -> List[TaskSearchEntry]:
        active_vectors = {k: v for k, v in vectors.items() if v is not None}
        if not active_vectors:
            return []

        async def _query_one(field_name: str, query_vector: List[float]) -> list:
            base = self.collection.where(filter=FieldFilter("user_id", "==", user_id))
            if not show_completed:
                base = base.where(filter=FieldFilter("status", "!=", TaskStatus.COMPLETED.value))
            if list_id is not None:
                base = base.where(filter=FieldFilter("list_id", "==", list_id))
            vq = base.find_nearest(
                vector_field=field_name,
                query_vector=query_vector,
                distance_measure=DistanceMeasure.COSINE,
                limit=limit * 2,
                distance_threshold=_MAX_COSINE_DISTANCE,
            )
            async with _FIND_NEAREST_SEMAPHORE:
                return await vq.get()

        tasks_list = [
            asyncio.create_task(_query_one(field, vec))
            for field, vec in active_vectors.items()
        ]
        results_lists = await asyncio.gather(*tasks_list, return_exceptions=True)

        docs_by_id: Dict[str, dict] = {}
        scores: Dict[str, float] = {}

        for query_results in results_lists:
            if isinstance(query_results, Exception):
                logger.error(f"💥 TaskSearchIndex.find_nearest query failed: {query_results}")
                continue
            for rank, doc in enumerate(query_results):
                doc_id = doc.id
                if doc_id not in docs_by_id:
                    docs_by_id[doc_id] = doc.to_dict()
                scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (_RRF_K + rank + 1)

        top_ids = sorted(scores, key=lambda x: scores[x], reverse=True)[:limit]

        entries: List[TaskSearchEntry] = []
        for eid in top_ids:
            try:
                entries.append(self._to_domain(docs_by_id[eid]))
            except Exception as exc:
                logger.warning(f"⚠️ TaskSearchIndex: failed to parse doc {eid}: {exc}")

        return entries

    # ------------------------------------------------------------------
    # TaskSearchIndex — get_by_short_id
    # ------------------------------------------------------------------

    async def get_by_short_id(self, user_id: str, short_id: str) -> Optional[TaskSearchEntry]:
        """Return the index entry matching user_id + short_id, or None."""
        query = (
            self.collection
            .where(filter=FieldFilter("user_id", "==", user_id))
            .where(filter=FieldFilter("short_id", "==", short_id))
            .limit(1)
        )
        docs = await query.get()
        if not docs:
            return None
        try:
            return self._to_domain(docs[0].to_dict())
        except Exception as exc:
            logger.warning(f"⚠️ TaskSearchIndex.get_by_short_id: failed to parse doc: {exc}")
            return None

    # ------------------------------------------------------------------
    # TaskSearchIndex — delete_all_for_user
    # ------------------------------------------------------------------

    async def delete_all_for_user(self, user_id: str) -> None:
        """Batch delete all task index entries for a user (on disconnect)."""
        query = self.collection.where(filter=FieldFilter("user_id", "==", user_id))
        docs = await query.get()
        chunk_size = 500
        for i in range(0, len(docs), chunk_size):
            batch = self.db.batch()
            for doc in docs[i : i + chunk_size]:
                batch.delete(doc.reference)
            await batch.commit()
        logger.info(f"📋 TaskSearchIndex: deleted {len(docs)} entries for user {user_id[:8]}")

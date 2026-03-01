"""
FirestoreIndexedEmailRepository — stores and searches indexed email facts.
See docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md §2.1.2.

Collections:
  {env}_domain_email_facts_v1  — email fact documents (doc ID = {user_id}_{email_id})
  {env}_email_indexing_state   — per-user/provider cursor (doc ID = {user_id}_{provider})
"""

import asyncio
from datetime import datetime
from typing import Dict, List, Optional

from google.cloud.firestore import FieldFilter
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector

from ..config.environment import EnvironmentConfig
from ..domain.email import IndexedEmail, IndexingState
from ..ports.indexed_email_repository import IndexedEmailRepository
from ..utils.logger import logger

# Semaphore: cap concurrent Firestore find_nearest calls for email collection.
# Email search fires up to 4 parallel vector queries; keep below quota threshold.
_EMAIL_FIND_NEAREST_SEMAPHORE = asyncio.Semaphore(10)

_VECTOR_FIELDS = ("vector", "tags_vector", "metadata_vector", "attachments_vector")

# RRF constant — standard value used throughout the codebase
_RRF_K = 60

# Cosine distance threshold: results farther than this are discarded by Firestore
# before RRF scoring. Cosine distance: 0.0 = identical, 1.0 = orthogonal.
_MAX_COSINE_DISTANCE = 0.4


class FirestoreIndexedEmailRepository(IndexedEmailRepository):

    def __init__(self, db_client, env_config: EnvironmentConfig):
        self.db = db_client

        facts_col = env_config.domain_email_facts_collection
        self.collection = self.db.collection(facts_col)

        state_col = env_config.email_indexing_state_collection
        self.indexing_state_col = self.db.collection(state_col)

        logger.info(
            f"📂 IndexedEmail repository initialized. "
            f"Facts: {facts_col}, State: {state_col}"
        )

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _wrap_vectors(data: dict) -> dict:
        """Wrap List[float] fields with Vector() for correct Firestore serialization."""
        for field in _VECTOR_FIELDS:
            if data.get(field) is not None:
                data[field] = Vector(data[field])
        return data

    @staticmethod
    def _unwrap_vectors(data: dict) -> dict:
        """Convert Firestore Vector objects back to List[float] for domain model."""
        for field in _VECTOR_FIELDS:
            v = data.get(field)
            if v is not None and not isinstance(v, list):
                data[field] = list(v)
        return data

    @staticmethod
    def _strip_tz(dt):
        """Convert a Firestore datetime to a plain naive datetime.

        Firestore returns DatetimeWithNanoseconds (a datetime subclass).
        Calling .replace(tzinfo=None) on it bypasses __init__ and leaves
        ._nanosecond unset, which crashes Firestore serialization on write-back.
        Always construct a fresh plain datetime to avoid this.
        """
        if dt is None:
            return None
        return datetime(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, dt.microsecond)

    def _to_domain(self, data: dict) -> IndexedEmail:
        self._unwrap_vectors(data)
        for field in ("indexed_at", "email_date", "consolidated_at"):
            if field in data:
                data[field] = self._strip_tz(data[field])
        return IndexedEmail(**data)

    # ------------------------------------------------------------------
    # save_batch
    # ------------------------------------------------------------------

    async def save_batch(self, emails: List[IndexedEmail]) -> int:
        """Upsert batch. Chunks automatically at Firestore's 500-write limit."""
        if not emails:
            return 0

        written = 0
        chunk_size = 500
        for i in range(0, len(emails), chunk_size):
            chunk = emails[i : i + chunk_size]
            batch = self.db.batch()
            for email in chunk:
                data = email.model_dump()
                self._wrap_vectors(data)
                doc_ref = self.collection.document(f"{email.user_id}_{email.email_id}")
                batch.set(doc_ref, data)
                written += 1
            await batch.commit()

        logger.info(f"💾 [IndexedEmail] save_batch: {written} docs written")
        return written

    # ------------------------------------------------------------------
    # find_nearest (multi-vector RRF)
    # ------------------------------------------------------------------

    async def find_nearest(
        self,
        user_id: str,
        vectors: Dict[str, List[float]],
        limit: int = 10,
        state: str = "current",
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> List[IndexedEmail]:
        """
        Multi-vector RRF search. For each key in `vectors`, fires a separate
        Firestore find_nearest query then combines results via RRF.
        date_from / date_to: optional pre-filter on email_date field.
        """
        active_vectors = {k: v for k, v in vectors.items() if v is not None}
        if not active_vectors:
            return []

        async def _query_one(
            field_name: str, query_vector: List[float]
        ) -> List:
            base = (
                self.collection
                .where(filter=FieldFilter("user_id", "==", user_id))
                .where(filter=FieldFilter("state", "==", state))
            )
            if date_from is not None:
                base = base.where(filter=FieldFilter("email_date", ">=", date_from))
            if date_to is not None:
                base = base.where(filter=FieldFilter("email_date", "<=", date_to))
            vq = base.find_nearest(
                    vector_field=field_name,
                    query_vector=query_vector,
                    distance_measure=DistanceMeasure.COSINE,
                    limit=limit * 2,  # Extra headroom for RRF merging
                    distance_threshold=_MAX_COSINE_DISTANCE,
                )
            t0 = asyncio.get_event_loop().time()
            async with _EMAIL_FIND_NEAREST_SEMAPHORE:
                docs = await vq.get()
            elapsed = int(1000 * (asyncio.get_event_loop().time() - t0))
            logger.info(
                f"🔍 [email.find_nearest] field={field_name} "
                f"results={len(docs)} elapsed={elapsed}ms"
            )
            return docs

        # Fire all vector queries in parallel
        tasks = [
            asyncio.create_task(_query_one(field, vec))
            for field, vec in active_vectors.items()
        ]
        results_lists = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect docs and compute RRF scores
        docs_by_id: Dict[str, dict] = {}
        scores: Dict[str, float] = {}

        for query_results in results_lists:
            if isinstance(query_results, Exception):
                logger.error(f"💥 [email.find_nearest] query failed: {query_results}")
                continue
            for rank, doc in enumerate(query_results):
                doc_id = doc.id  # composite: {user_id}_{email_id}
                if doc_id not in docs_by_id:
                    data = doc.to_dict()  # email_id field already correct in document
                    docs_by_id[doc_id] = data
                scores[doc_id] = (
                    scores.get(doc_id, 0.0) + 1.0 / (_RRF_K + rank + 1)
                )

        top_ids = sorted(scores, key=lambda x: scores[x], reverse=True)[:limit]

        emails = []
        for eid in top_ids:
            try:
                emails.append(self._to_domain(docs_by_id[eid]))
            except Exception as exc:
                logger.error(f"💥 [email.find_nearest] failed to parse {eid}: {exc}")

        return emails

    # ------------------------------------------------------------------
    # Indexing state (cursor tracking)
    # ------------------------------------------------------------------

    async def get_indexing_state(
        self, user_id: str, provider: str
    ) -> Optional[IndexingState]:
        doc_id = f"{user_id}_{provider}"
        doc = await self.indexing_state_col.document(doc_id).get()
        if not doc.exists:
            return None
        data = doc.to_dict()
        return IndexingState(
            user_id=data["user_id"],
            provider=data["provider"],
            indexed_through=self._strip_tz(data.get("indexed_through")),
            oldest_indexed_through=self._strip_tz(data.get("oldest_indexed_through")),
            cursor_reindex=self._strip_tz(data.get("cursor_reindex")),
        )

    async def update_indexing_state(self, state: IndexingState) -> None:
        doc_id = f"{state.user_id}_{state.provider}"
        await self.indexing_state_col.document(doc_id).set({
            "user_id": state.user_id,
            "provider": state.provider,
            "indexed_through": state.indexed_through,
            "oldest_indexed_through": state.oldest_indexed_through,
            "cursor_reindex": state.cursor_reindex,
        })
        logger.debug(
            f"📌 Indexing state updated: user={state.user_id[:8]} "
            f"provider={state.provider} "
            f"indexed_through={state.indexed_through} "
            f"oldest={state.oldest_indexed_through} "
            f"cursor_reindex={state.cursor_reindex}"
        )

    async def clear_indexing_state(self, user_id: str, provider: str) -> None:
        doc_id = f"{user_id}_{provider}"
        await self.indexing_state_col.document(doc_id).delete()
        logger.info(
            f"🗑️ Indexing state cleared: user={user_id[:8]} provider={provider}"
        )

    # ------------------------------------------------------------------
    # Count / delete
    # ------------------------------------------------------------------

    async def count_by_user(
        self, user_id: str, provider: Optional[str] = None
    ) -> int:
        query = self.collection.where(filter=FieldFilter("user_id", "==", user_id))
        if provider:
            query = query.where(filter=FieldFilter("source", "==", provider))
        count = 0
        async for _ in query.stream():
            count += 1
        return count

    async def delete_by_user(self, user_id: str) -> None:
        """Batch-deletes all indexed facts for user. Chunks at 500 (Firestore limit)."""
        deleted = 0
        while True:
            docs = await (
                self.collection
                .where(filter=FieldFilter("user_id", "==", user_id))
                .limit(500)
                .get()
            )
            if not docs:
                break
            batch = self.db.batch()
            for doc in docs:
                batch.delete(doc.reference)
                deleted += 1
            await batch.commit()
        logger.info(f"🗑️ Deleted {deleted} indexed email facts for user={user_id[:8]}")

    # ------------------------------------------------------------------
    # Consolidation support
    # ------------------------------------------------------------------

    async def get_unconsolidated_batch(
        self, user_id: str, limit: int = 200
    ) -> List[IndexedEmail]:
        """WHERE consolidated_at IS NULL AND user_id = X ORDER BY indexed_at ASC LIMIT N."""
        query = (
            self.collection
            .where(filter=FieldFilter("user_id", "==", user_id))
            .where(filter=FieldFilter("consolidated_at", "==", None))
            .order_by("indexed_at")
            .limit(limit)
        )
        docs = await query.get()
        emails = []
        for doc in docs:
            data = doc.to_dict()  # email_id field already correct in document
            try:
                emails.append(self._to_domain(data))
            except Exception as exc:
                logger.error(
                    f"💥 [get_unconsolidated_batch] failed to parse {doc.id}: {exc}"
                )
        return emails

    async def mark_consolidated(
        self, user_id: str, email_ids: List[str], consolidated_at: datetime
    ) -> None:
        """Batch-updates consolidated_at on processed IDs. Chunks at 500."""
        chunk_size = 500
        for i in range(0, len(email_ids), chunk_size):
            chunk = email_ids[i : i + chunk_size]
            batch = self.db.batch()
            for email_id in chunk:
                doc_ref = self.collection.document(f"{user_id}_{email_id}")
                batch.update(doc_ref, {"consolidated_at": consolidated_at})
            await batch.commit()
        logger.debug(
            f"📌 Marked {len(email_ids)} emails consolidated at {consolidated_at}"
        )

    # ------------------------------------------------------------------
    # Embedding repair
    # ------------------------------------------------------------------

    async def get_pending_embeddings(self, limit: int = 100) -> List[IndexedEmail]:
        """WHERE embedding_pending=True LIMIT N (cross-user, for repair service)."""
        query = (
            self.collection
            .where(filter=FieldFilter("embedding_pending", "==", True))
            .limit(limit)
        )
        docs = await query.get()
        emails = []
        for doc in docs:
            data = doc.to_dict()  # email_id field already correct in document
            try:
                emails.append(self._to_domain(data))
            except Exception as exc:
                logger.error(
                    f"💥 [get_pending_embeddings] failed to parse {doc.id}: {exc}"
                )
        return emails

    async def update_vectors(
        self, user_id: str, email_id: str, vectors: Dict[str, List[float]]
    ) -> None:
        """Partial update: write computed vectors and clear embedding_pending flag."""
        data: dict = {"embedding_pending": False}
        for field, vec in vectors.items():
            if vec is not None:
                data[field] = Vector(vec)
        await self.collection.document(f"{user_id}_{email_id}").update(data)
        logger.debug(f"📐 Vectors updated for email {email_id}")

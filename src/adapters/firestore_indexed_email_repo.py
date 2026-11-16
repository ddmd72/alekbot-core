"""
FirestoreIndexedEmailRepository — stores and searches indexed email facts.
See docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md §2.1.2.

Collections:
  {env}_domain_email_facts_v1  — email fact documents (doc ID = {user_id}_{email_id})
  {env}_email_indexing_state   — per-user/provider cursor (doc ID = {user_id}_{provider})
"""

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Union

from google.api_core.exceptions import NotFound
from google.cloud.firestore import FieldFilter

from ..config.environment import EnvironmentConfig
from ..domain.email import IndexedEmail, IndexingState
from ..domain.email_memory_mail import MemoryMail
from ..domain.reconnect_policy import Cleared
from ..ports.email_memory_batches import EmailMemoryBatches
from ..ports.indexed_email_repository import IndexedEmailRepository
from ..utils.logger import logger

# What Stage 3 reads of a mail: the v1 record, and the card fields `card_from_document` needs.
_CARD_READ_FIELDS = ("card_state", "card_version", "template_key", "card")
_MEMORY_READ_FIELDS = tuple(IndexedEmail.model_fields) + _CARD_READ_FIELDS


class FirestoreIndexedEmailRepository(IndexedEmailRepository, EmailMemoryBatches):
    """Firestore mechanics (the port states the observable contract):
    - Facts: `EnvironmentConfig.domain_email_facts_collection` (`{prefix}domain_email_facts_v1`), body
      `IndexedEmail.model_dump()`; cursors:
      `EnvironmentConfig.email_indexing_state_collection`, document id `{user_id}_{provider}`; `update_indexing_state`
      updates only the cursor fields, `set_reconnect_flags` only `reconnect_notified` / `disconnected_by_user_at`
      (an `update`, so a missing state stays missing); `list_indexing_user_ids` is one equality query on `provider`.
    - `save_batch` and `mark_consolidated` write in batches of 500 (`batch.set` of the whole document / `batch.update`);
      `delete_by_user` deletes 500 per query until none is left.
    - `get_consolidation_marks`: `get_all` of the `{user_id}_{email_id}` refs (500 per call) with
      `field_paths=["consolidated_at"]`; a missing document is left out.
    - `get_unconsolidated_batch` needs the composite index (user_id, consolidated_at, indexed_at); a document without a
      `consolidated_at` field never matches `consolidated_at == null`.
    - `memory_batch` (the `EmailMemoryBatches` port) runs two queries, each ordered by `indexed_at` and limited: the
      settled one (`card_state in`) needs (user_id, consolidated_at, card_state, indexed_at), the stale one
      (`indexed_at <`) the v1 index above; merged by document id. Both select the v1 fields plus the card
      fields `card_state`, `card_version`, `template_key`, `card`.
    - Datetimes read back are rebuilt as plain naive datetimes (a `DatetimeWithNanoseconds` must not be written back).
    """

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
                doc_ref = self.collection.document(f"{email.user_id}_{email.email_id}")
                batch.set(doc_ref, data)
                written += 1
            await batch.commit()

        logger.info(f"💾 [IndexedEmail] save_batch: {written} docs written")
        return written

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
            reconnect_notified=bool(data.get("reconnect_notified", False)),
            disconnected_by_user_at=self._strip_tz(data.get("disconnected_by_user_at")),
        )

    async def set_reconnect_flags(
        self, user_id: str, provider: str, *,
        reconnect_notified: Optional[bool] = None,
        disconnected_by_user_at: Union[datetime, Cleared, None] = None,
    ) -> None:
        changes: Dict[str, Any] = {}
        if reconnect_notified is not None:
            changes["reconnect_notified"] = reconnect_notified
        if disconnected_by_user_at is Cleared.MARK:
            changes["disconnected_by_user_at"] = None
        elif disconnected_by_user_at is not None:
            changes["disconnected_by_user_at"] = disconnected_by_user_at
        if not changes:
            return
        try:
            await self.indexing_state_col.document(f"{user_id}_{provider}").update(changes)
        except NotFound:
            logger.debug(f"📌 No indexing state for user={user_id[:8]} provider={provider}; "
                         f"reconnect flags {sorted(changes)} not written")

    async def list_indexing_user_ids(self, provider: str) -> List[str]:
        query = self.indexing_state_col.where(filter=FieldFilter("provider", "==", provider)).select(["user_id"])
        ids = {snap.to_dict().get("user_id") async for snap in query.stream()}
        return sorted(i for i in ids if i)

    async def update_indexing_state(self, state: IndexingState) -> None:
        doc_id = f"{state.user_id}_{state.provider}"
        doc_ref = self.indexing_state_col.document(doc_id)
        data = {
            "user_id": state.user_id,
            "provider": state.provider,
            "indexed_through": state.indexed_through,
            "oldest_indexed_through": state.oldest_indexed_through,
            "cursor_reindex": state.cursor_reindex,
        }
        try:
            await doc_ref.update(data)
        except NotFound:
            await doc_ref.set(data)
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

    async def memory_batch(
        self, user_id: str, limit: int, settled: Sequence[str], stale_before: datetime
    ) -> List[MemoryMail]:
        unconsolidated = (
            self.collection
            .where(filter=FieldFilter("user_id", "==", user_id))
            .where(filter=FieldFilter("consolidated_at", "==", None))
        )
        queries = [
            unconsolidated.where(filter=FieldFilter("indexed_at", "<", stale_before)),
        ]
        if settled:
            queries.append(unconsolidated.where(filter=FieldFilter("card_state", "in", list(settled))))
        results = await asyncio.gather(*[
            q.order_by("indexed_at").select(list(_MEMORY_READ_FIELDS)).limit(limit).get() for q in queries
        ])
        mails: Dict[str, MemoryMail] = {}
        for docs in results:
            for doc in docs:
                if doc.id in mails:
                    continue
                data = doc.to_dict() or {}
                card = {k: data.pop(k) for k in _CARD_READ_FIELDS if k in data}
                try:
                    email = self._to_domain(data)
                except Exception as exc:
                    logger.error(f"💥 [memory_batch] failed to parse {doc.id}: {exc}")
                    continue
                mails[doc.id] = MemoryMail(email, card.get("card_state") or "", card or None)
        return sorted(mails.values(), key=lambda m: m.email.indexed_at)[:limit]

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

    async def get_consolidation_marks(
        self, user_id: str, email_ids: List[str]
    ) -> Dict[str, Optional[datetime]]:
        """`get_all` on the documents `{user_id}_{email_id}`, reading only `consolidated_at`; 500 refs per call."""
        marks: Dict[str, Optional[datetime]] = {}
        prefix = f"{user_id}_"
        chunk_size = 500
        for i in range(0, len(email_ids), chunk_size):
            chunk = email_ids[i : i + chunk_size]
            refs = [self.collection.document(f"{prefix}{email_id}") for email_id in chunk]
            async for snap in self.db.get_all(refs, field_paths=["consolidated_at"]):
                if not snap.exists:
                    continue
                data = snap.to_dict() or {}
                marks[snap.id[len(prefix):]] = self._strip_tz(data.get("consolidated_at"))
        return marks

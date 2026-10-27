import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from google.api_core.exceptions import AlreadyExists, FailedPrecondition, NotFound
from google.cloud.firestore_v1.async_client import AsyncClient
from ..ports.dedup_store import DedupStore
from ..utils.logger import logger


class FirestoreDedupStore(DedupStore):
    """
    Universal Firestore-backed deduplication store for any platform.
    
    Session: 2026-02-09 Telegram Integration Phase 3
    Purpose: Platform-agnostic dedup store (Telegram, Slack, future platforms)

    Documents `{created_at: epoch float, expires_at: aware datetime}`; `expires_at` is the TTL policy field. The 48 h
    daily-review marks and the 72 h OpenAI webhook claims share `{prefix}worker_task_dedup` with the TaskClaimStore
    claims (claim keys never contain `:`, mark keys always do); the 1 h Telegram inline marks live in
    `{prefix}event_dedup`. A claim is a `create` (AlreadyExists = taken); an expired, unpurged mark is rewritten under a
    `last_update_time` precondition. An empty or None key is never marked (no caller passes one); a naive `expires_at`
    is read as UTC (defensive: the client returns aware values).
    """

    def __init__(self, db_client: AsyncClient, collection_name: str, ttl_seconds: int = 3600):
        """
        Initialize dedup store with explicit collection name.
        
        Args:
            db_client: Firestore AsyncClient
            collection_name: Full collection name (e.g., "dev_event_dedup" or "telegram_dedup")
            ttl_seconds: TTL for dedup entries (default: 1 hour)
        """
        self.collection_name = collection_name
        self.db_client = db_client
        self.ttl_seconds = ttl_seconds

    async def is_duplicate(self, event_id: Optional[str]) -> bool:
        """
        Check if event was already processed.
        Note: This is non-atomic. Use mark_processed_atomic for race-condition safety.
        """
        if not event_id:
            return False
        doc_ref = self.db_client.collection(self.collection_name).document(event_id)
        doc = await doc_ref.get()
        if not doc.exists:
            return False
        return self._is_live(doc.to_dict() or {})

    def _is_live(self, data: dict) -> bool:
        """A mark is live until its own expires_at; old docs without it fall back to created_at + instance TTL."""
        expires_at = data.get("expires_at")
        if isinstance(expires_at, datetime):
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) < expires_at
        return time.time() - data.get("created_at", 0) <= self.ttl_seconds

    def _make_doc(self) -> dict:
        """Build dedup document with expires_at for Firestore TTL policy."""
        now = datetime.now(timezone.utc)
        return {
            "created_at": time.time(),
            "expires_at": now + timedelta(seconds=self.ttl_seconds),
        }

    async def mark_processed(self, event_id: Optional[str]) -> None:
        """Legacy non-atomic mark."""
        if not event_id:
            return
        doc_ref = self.db_client.collection(self.collection_name).document(event_id)
        await doc_ref.set(self._make_doc())
        logger.debug(f"🧹 Dedup mark stored for event {event_id[:12]}...")

    async def try_mark_processed(self, event_id: Optional[str]) -> bool:
        """
        Atomic claim via create(); an expired, not-yet-purged mark is re-claimed with a
        last_update_time-guarded rewrite. True only when this caller now holds a live mark.
        Errors other than "already marked" propagate.
        """
        if not event_id:
            return False
        doc_ref = self.db_client.collection(self.collection_name).document(event_id)
        try:
            claimed = await self._claim(doc_ref, event_id, retry_create=True)
        except Exception as e:
            logger.error(f"Dedup claim failed for event {event_id[:12]}...: {e}")
            raise
        if not claimed:
            logger.info(f"⏭️ Atomic check confirmed duplicate for event {event_id[:12]}...")
        return claimed

    async def _claim(self, doc_ref: Any, event_id: str, retry_create: bool) -> bool:
        try:
            await doc_ref.create(self._make_doc())
            logger.debug(f"✅ Atomic dedup mark stored for event {event_id[:12]}...")
            return True
        except AlreadyExists:
            logger.debug(f"Dedup mark exists for event {event_id[:12]}..., checking liveness")
        snap = await doc_ref.get()
        if not snap.exists:
            # Released between our create and get: one more attempt, then give way.
            return await self._claim(doc_ref, event_id, retry_create=False) if retry_create else False
        if self._is_live(snap.to_dict() or {}):
            return False
        try:
            await doc_ref.update(
                self._make_doc(),
                option=self.db_client.write_option(last_update_time=snap.update_time),
            )
        except FailedPrecondition:
            return False  # another caller re-claimed it first
        except NotFound:
            return await self._claim(doc_ref, event_id, retry_create=False) if retry_create else False
        logger.debug(f"♻️ Expired dedup mark re-claimed for event {event_id[:12]}...")
        return True

    async def release(self, event_id: Optional[str]) -> None:
        """Delete the mark so a retry of a failed attempt is processed, not skipped."""
        if not event_id:
            return
        await self.db_client.collection(self.collection_name).document(event_id).delete()
        logger.debug(f"🧹 Dedup mark released for event {event_id[:12]}...")

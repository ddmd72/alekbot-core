import time
from typing import Optional
from google.cloud.firestore_v1.async_client import AsyncClient
from ..utils.logger import logger


class FirestoreDedupStore:
    """
    Universal Firestore-backed deduplication store for any platform.
    
    Session: 2026-02-09 Telegram Integration Phase 3
    Purpose: Platform-agnostic dedup store (Telegram, Slack, future platforms)
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
        data = doc.to_dict() or {}
        created_at = data.get("created_at", 0)
        if time.time() - created_at > self.ttl_seconds:
            return False
        return True

    async def mark_processed(self, event_id: Optional[str]) -> None:
        """Legacy non-atomic mark."""
        if not event_id:
            return
        doc_ref = self.db_client.collection(self.collection_name).document(event_id)
        await doc_ref.set({
            "created_at": time.time()
        })
        logger.debug(f"🧹 Dedup mark stored for event {event_id[:12]}...")

    async def try_mark_processed(self, event_id: Optional[str]) -> bool:
        """
        Atomic check-and-set using Firestore create().
        Returns True if successfully marked (first time), False if already exists.
        """
        if not event_id:
            return False
        try:
            doc_ref = self.db_client.collection(self.collection_name).document(event_id)
            # create() fails if document already exists
            await doc_ref.create({
                "created_at": time.time()
            })
            logger.debug(f"✅ Atomic dedup mark stored for event {event_id[:12]}...")
            return True
        except Exception:
            # Document already exists or other Firestore error
            # We check if it's actually a duplicate or just a transient error
            is_dup = await self.is_duplicate(event_id)
            if is_dup:
                logger.info(f"⏭️ Atomic check confirmed duplicate for event {event_id[:12]}...")
            return not is_dup


class FirestoreEventDedupStore(FirestoreDedupStore):
    """
    Backward-compatible Slack event dedup store.
    
    Legacy class for Slack adapter. Uses collection_prefix pattern.
    New code should use FirestoreDedupStore directly with explicit collection_name.
    """

    def __init__(self, db_client: AsyncClient, collection_prefix: str, ttl_seconds: int = 3600):
        # ADR-006: Use semantic collection name if collection_prefix matches pattern
        # Fallback to old behavior if simple prefix string is passed
        if collection_prefix.endswith("event_dedup"):
            collection_name = collection_prefix
        else:
            collection_name = f"{collection_prefix}event_dedup"
            
        super().__init__(db_client, collection_name, ttl_seconds)

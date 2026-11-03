"""
FirestoreTranscriptCache — TranscriptCachePort on Firestore (docs/10_rfcs/EXTERNAL_FILES_RFC.md §4.4 P21).

`{prefix}audio_transcripts/{account_id}:{key}`: `text`, `account_id`, `written_at` (UTC, at the put) and `purge_after`
(the caller's, aware). The collection's TTL policy is on `purge_after` (docs/07_deployment/README.md); TTL deletion
lags, so `get` treats an entry whose `purge_after` has passed as absent. A put replaces the entry whole. The injected
client already carries `database=`.
"""
from datetime import datetime, timezone
from typing import Any, Optional

from ..ports.transcript_cache_port import TranscriptCachePort


class FirestoreTranscriptCache(TranscriptCachePort):

    def __init__(self, db_client: Any, collection: str) -> None:
        self._db = db_client
        self._collection = collection

    def _doc(self, account_id: str, key: str) -> Any:
        if not account_id or not key or "/" in account_id or "/" in key:
            raise ValueError("a transcript entry needs an account id and a key, neither containing '/'")
        return self._db.collection(self._collection).document(f"{account_id}:{key}")

    async def get(self, account_id: str, key: str) -> Optional[str]:
        snap = await self._doc(account_id, key).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        purge_after = data.get("purge_after")
        if not isinstance(purge_after, datetime) or purge_after <= datetime.now(timezone.utc):
            return None
        text = data.get("text")
        return text if isinstance(text, str) else None

    async def put(self, account_id: str, key: str, text: str, *, purge_after: datetime) -> None:
        if purge_after.tzinfo is None:
            raise ValueError("purge_after must be an aware datetime")
        await self._doc(account_id, key).set({
            "text": text, "account_id": account_id, "written_at": datetime.now(timezone.utc),
            "purge_after": purge_after,
        })

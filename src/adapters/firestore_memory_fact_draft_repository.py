"""
FirestoreMemoryFactDraftRepository — memory facts proposed by Smart, pending the owner's `$memory save`.

Collection: EnvironmentConfig.memory_fact_drafts_collection   doc id = {user_id}:{code}
  draft doc: user_id, facts: [{text, source}], origin_session_id, created_at, expires_at   (TTL policy on `expires_at`)
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from google.api_core.exceptions import AlreadyExists
from pydantic import ValidationError

from ..config.environment import EnvironmentConfig
from ..domain.memory_fact import MemoryFactDraft
from ..ports.memory_fact_draft_repository import MemoryFactDraftRepository
from ..utils.logger import logger

DRAFT_TTL = timedelta(days=7)


def _expired(data: dict, now: datetime) -> bool:
    expires_at = data.get("expires_at")
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= now


class FirestoreMemoryFactDraftRepository(MemoryFactDraftRepository):
    """Firestore mechanics behind the port's contract.

    Collection `EnvironmentConfig.memory_fact_drafts_collection`, document id `{user_id}:{code}`. A Firestore TTL policy
    on `expires_at` removes old drafts; its sweep lags up to a day, so `get_draft` re-checks the expiry itself.
    `create_draft` is one `create` (it fails with AlreadyExists when the document exists). No query is issued, so no
    index is needed.
    """

    def __init__(self, db_client, env_config: EnvironmentConfig):
        self._drafts = db_client.collection(env_config.memory_fact_drafts_collection)

    def _ref(self, user_id: str, code: str):
        return self._drafts.document(f"{user_id}:{code}")

    async def create_draft(self, user_id: str, code: str, draft: MemoryFactDraft) -> bool:
        now = datetime.now(timezone.utc)
        try:
            await self._ref(user_id, code).create({
                "user_id": user_id,
                "facts": [f.model_dump() for f in draft.facts],
                "origin_session_id": draft.origin_session_id,
                "created_at": now,
                "expires_at": now + DRAFT_TTL,
            })
            return True
        except AlreadyExists:
            return False

    async def get_draft(self, user_id: str, code: str) -> Optional[MemoryFactDraft]:
        snapshot = await self._ref(user_id, code).get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        # TTL deletion lags up to a day; an expired draft is gone as far as callers are concerned.
        if _expired(data, datetime.now(timezone.utc)):
            return None
        try:
            return MemoryFactDraft(
                facts=data.get("facts") or [], origin_session_id=data.get("origin_session_id"),
            )
        except ValidationError as e:
            logger.error("❌ [MemoryFacts] Skipping corrupt draft %s…:%s: %s", user_id[:8], code, e)
            return None

    async def delete_draft(self, user_id: str, code: str) -> None:
        await self._ref(user_id, code).delete()

"""
FirestoreCompanionCacheRepository — Firestore implementation of
CompanionCacheRepository. One doc per session_id, full-replace writes
(no merge=True — the assembler always reads the whole doc back, there is
no partial-field caller).
"""
from typing import Optional

from ..config.environment import EnvironmentConfig
from ..ports.companion_cache_repository import CompanionCacheRepository
from ..utils.logger import logger


class FirestoreCompanionCacheRepository(CompanionCacheRepository):

    def __init__(self, db_client, env_config: EnvironmentConfig):
        self.db = db_client
        self.collection = self.db.collection(env_config.companion_context_cache_collection)

    async def get_summary(self, session_id: str) -> Optional[str]:
        doc = await self.collection.document(session_id).get()
        if not doc.exists:
            return None
        return (doc.to_dict() or {}).get("summary")

    async def save_summary(self, session_id: str, account_id: str, summary: str) -> None:
        await self.collection.document(session_id).set({
            "session_id": session_id,
            "account_id": account_id,
            "summary": summary,
        })
        logger.info("🧑‍🏫 [CompanionCache] summary saved for session=%s", session_id)

"""
FirestoreEmailExclusionsAdapter — manages sender/domain/subject exclusion patterns.
See docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md §2.1.2.

Collection: {env}_email_exclusions
Doc ID: {user_id}_{pattern_type}_{pattern} (deterministic, enables idempotent upserts)
"""

import hashlib
from typing import List

from google.cloud.firestore import FieldFilter

from ..config.environment import EnvironmentConfig
from ..domain.email import EmailExclusion
from ..ports.email_exclusions_port import EmailExclusionsPort
from ..utils.logger import logger


def _exclusion_doc_id(user_id: str, pattern_type: str, pattern: str) -> str:
    """Deterministic, URL-safe doc ID for idempotent upserts."""
    raw = f"{user_id}|{pattern_type}|{pattern}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class FirestoreEmailExclusionsAdapter(EmailExclusionsPort):

    def __init__(self, db_client, env_config: EnvironmentConfig):
        self.db = db_client
        collection_name = env_config.email_exclusions_collection
        self.collection = self.db.collection(collection_name)
        logger.info(
            f"📂 EmailExclusions adapter initialized. Collection: {collection_name}"
        )

    def _to_domain(self, doc_id: str, data: dict) -> EmailExclusion:
        return EmailExclusion(
            exclusion_id=doc_id,
            user_id=data["user_id"],
            pattern_type=data["pattern_type"],
            pattern=data["pattern"],
            reason=data["reason"],
            created_at=data["created_at"],
        )

    async def get_exclusions(self, user_id: str) -> List[EmailExclusion]:
        """Load all active exclusion patterns for user (fast pre-filter before LLM)."""
        query = self.collection.where(filter=FieldFilter("user_id", "==", user_id))
        exclusions = []
        async for doc in query.stream():
            try:
                exclusions.append(self._to_domain(doc.id, doc.to_dict()))
            except Exception as exc:
                logger.error(
                    f"💥 [EmailExclusions] failed to parse {doc.id}: {exc}"
                )
        return exclusions

    async def add_exclusions(self, exclusions: List[EmailExclusion]) -> None:
        """Persist auto-detected patterns. Idempotent: upserts by deterministic doc ID."""
        if not exclusions:
            return
        batch = self.db.batch()
        for ex in exclusions:
            doc_id = _exclusion_doc_id(ex.user_id, ex.pattern_type, ex.pattern)
            doc_ref = self.collection.document(doc_id)
            data = ex.model_dump(exclude={"exclusion_id"})
            batch.set(doc_ref, data)
        await batch.commit()
        logger.info(f"💾 Added {len(exclusions)} email exclusions")

    async def delete_exclusion(self, user_id: str, exclusion_id: str) -> None:
        """User removes a pattern via Cabinet UI."""
        await self.collection.document(exclusion_id).delete()
        logger.info(
            f"🗑️ Email exclusion deleted: user={user_id[:8]} id={exclusion_id}"
        )

    async def list_exclusions(self, user_id: str) -> List[EmailExclusion]:
        """For Cabinet display. Semantically distinct from get_exclusions."""
        return await self.get_exclusions(user_id)

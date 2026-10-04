"""
FirestoreSkillRepository — custom skills per user.

Collection: EnvironmentConfig.skills_collection   doc id = {user_id}:{name}
  index doc: user_id, account_id, name, description, body, current, updated_at
             (current version denormalized, so listing needs no subcollection reads)
  versions/v<n>: name, description, body, saved_at   (immutable history)

Collection: EnvironmentConfig.skill_drafts_collection   doc id = {user_id}:{code}
  draft doc: user_id, name, description, body, created_at
             (holds a model-authored skill pending the owner's `$skill save` confirmation;
             `save_version(..., consume_drafts_named=...)` deletes drafts by name once saved)
"""

from datetime import datetime, timezone
from typing import List, Optional

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore
from google.cloud.firestore import FieldFilter
from pydantic import ValidationError

from ..config.environment import EnvironmentConfig
from ..domain.exceptions import SkillCapExceeded
from ..domain.skill import Skill
from ..ports.skill_repository import SkillRepository
from ..utils.logger import logger


class FirestoreSkillRepository(SkillRepository):

    def __init__(self, db_client, env_config: EnvironmentConfig):
        self._db = db_client
        # Created before _col so the collections-created-last assertion in the existing
        # skills-collection test (test_firestore_skill_repository.py:50) stays accurate.
        self._drafts = db_client.collection(env_config.skill_drafts_collection)
        self._col = db_client.collection(env_config.skills_collection)

    def _user_query(self, user_id: str):
        return self._col.where(filter=FieldFilter("user_id", "==", user_id))

    def _drafts_named_query(self, user_id: str, name: str):
        return (
            self._drafts
            .where(filter=FieldFilter("user_id", "==", user_id))
            .where(filter=FieldFilter("name", "==", name))
        )

    async def list_current(self, user_id: str) -> List[Skill]:
        skills: List[Skill] = []
        for doc in await self._user_query(user_id).get():
            data = doc.to_dict() or {}
            try:
                skills.append(Skill(
                    name=data["name"],
                    description=data["description"],
                    body=data["body"],
                    version=data["current"],
                ))
            except (KeyError, ValidationError) as e:
                # Truncate the user_id half of "{user_id}:{name}" — PII, never log it in full.
                doc_id = getattr(doc, "id", "?")
                name_part = doc_id.split(":", 1)[1] if ":" in doc_id else "?"
                logger.error("❌ [Skills] Skipping corrupt skill doc %s…:%s: %s", doc_id[:8], name_part, e)
        return sorted(skills, key=lambda s: s.name)

    async def save_version(
        self, user_id: str, account_id: str, skill: Skill, cap: int,
        consume_drafts_named: Optional[str] = None,
    ) -> int:
        doc_ref = self._col.document(f"{user_id}:{skill.name}")
        user_query = self._user_query(user_id)
        drafts_query = self._drafts_named_query(user_id, consume_drafts_named) if consume_drafts_named else None
        transaction = self._db.transaction()

        @firestore.async_transactional
        async def _txn(txn) -> int:
            snapshot = await doc_ref.get(transaction=txn)
            if snapshot.exists:
                version = int(snapshot.to_dict()["current"]) + 1
            else:
                existing = await user_query.get(transaction=txn)
                if len(existing) >= cap:
                    raise SkillCapExceeded(f"user already has {len(existing)} skills (cap {cap})")
                version = 1
            # All reads (index doc, cap query, drafts query) happen before any write.
            drafts = await drafts_query.get(transaction=txn) if drafts_query is not None else []
            now = datetime.now(timezone.utc)
            txn.set(doc_ref.collection("versions").document(f"v{version}"), {
                "name": skill.name,
                "description": skill.description,
                "body": skill.body,
                "saved_at": now,
            })
            txn.set(doc_ref, {
                "user_id": user_id,
                "account_id": account_id,
                "name": skill.name,
                "description": skill.description,
                "body": skill.body,
                "current": version,
                "updated_at": now,
            })
            for d in drafts:
                txn.delete(d.reference)
            return version

        return await _txn(transaction)

    async def create_draft(self, user_id: str, code: str, skill: Skill) -> bool:
        try:
            await self._drafts.document(f"{user_id}:{code}").create({
                "user_id": user_id,
                "name": skill.name,
                "description": skill.description,
                "body": skill.body,
                "created_at": datetime.now(timezone.utc),
            })
            return True
        except AlreadyExists:
            return False

    async def get_draft(self, user_id: str, code: str) -> Optional[Skill]:
        snapshot = await self._drafts.document(f"{user_id}:{code}").get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        try:
            return Skill(
                name=data["name"],
                description=data["description"],
                body=data["body"],
            )
        except (KeyError, ValidationError) as e:
            logger.error("❌ [Skills] Skipping corrupt draft doc %s…:%s: %s", user_id[:8], code, e)
            return None

    async def delete_skill(self, user_id: str, name: str) -> bool:
        doc_ref = self._col.document(f"{user_id}:{name}")
        snapshot = await doc_ref.get()
        if not snapshot.exists:
            return False
        for version_doc in await doc_ref.collection("versions").get():
            await version_doc.reference.delete()
        await doc_ref.delete()
        return True

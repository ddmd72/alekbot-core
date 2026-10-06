"""
FirestoreSkillRepository — custom skills per user.

Collection: EnvironmentConfig.skills_collection   doc id = {user_id}:{name}
  index doc: user_id, account_id, name, description, body, current, updated_at,
             files: [{path, sha256, size}]
             (current version denormalized, so listing needs no subcollection reads)
  versions/v<n>: name, description, body, files, saved_at   (immutable history)
  files/{sha256}: content, size, created_at
             (content-addressed, shared by every version that lists the hash; NEVER `expires_at`)

Collection: EnvironmentConfig.skill_drafts_collection   doc id = {user_id}:{code}
  draft doc: user_id, name, description, body, files, staged: [sha256], created_at, expires_at
             (holds a model-authored skill pending the owner's `$skill save` confirmation;
             `save_version(..., consume_drafts_named=...)` deletes drafts by name once saved)
  draft_files/{sha256}: content, size, created_at, expires_at
             (content the skill does not hold yet; its own collection-group name so the TTL
             policy on `expires_at` never covers a skill's `files/`)
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Mapping, Optional

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore
from google.cloud.firestore import FieldFilter
from pydantic import ValidationError

from ..config.environment import EnvironmentConfig
from ..domain.exceptions import SkillCapExceeded, SkillDraftNotFound, SkillFileMissing
from ..domain.skill import Skill
from ..ports.skill_repository import SkillRepository
from ..utils.logger import logger

DRAFT_TTL = timedelta(days=30)
_FILES = "files"
_DRAFT_FILES = "draft_files"
_VERSIONS = "versions"
# Under Firestore's 500-writes-per-batch limit, with headroom.
_DELETE_CHUNK = 450


def _manifest(skill: Skill) -> list:
    return [f.model_dump() for f in skill.files]


def _skill_from(data: dict, version: int = 0) -> Skill:
    # Pass raw entries: a corrupt entry raises ValidationError, which the callers already
    # catch per document (a TypeError from SkillFile(**f) would take down the whole listing).
    return Skill(
        name=data["name"], description=data["description"], body=data["body"],
        version=version, files=data.get("files") or [],
    )


def _expired(data: dict, now: datetime) -> bool:
    expires_at = data.get("expires_at")
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= now


def _doc_label(doc_id: str) -> str:
    # Truncate the user_id half of "{user_id}:{rest}" — PII, never log it in full.
    rest = doc_id.split(":", 1)[1] if ":" in doc_id else "?"
    return f"{doc_id[:8]}…:{rest}"


class FirestoreSkillRepository(SkillRepository):

    def __init__(self, db_client, env_config: EnvironmentConfig):
        self._db = db_client
        # Drafts collection is resolved before the skills collection.
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
                skills.append(_skill_from(data, version=data["current"]))
            except (KeyError, ValidationError) as e:
                logger.error("❌ [Skills] Skipping corrupt skill doc %s: %s",
                             _doc_label(getattr(doc, "id", None) or "?"), e)
        return sorted(skills, key=lambda s: s.name)

    async def get_current(self, user_id: str, name: str) -> Optional[Skill]:
        snapshot = await self._col.document(f"{user_id}:{name}").get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        try:
            return _skill_from(data, version=data["current"])
        except (KeyError, ValidationError) as e:
            logger.error("❌ [Skills] Corrupt skill doc %s…:%s: %s", user_id[:8], name, e)
            return None

    async def get_file(self, user_id: str, name: str, sha256: str) -> Optional[str]:
        snapshot = await (
            self._col.document(f"{user_id}:{name}").collection(_FILES).document(sha256).get()
        )
        if not snapshot.exists:
            return None
        return (snapshot.to_dict() or {}).get("content")

    async def save_version(
        self, user_id: str, account_id: str, skill: Skill, cap: int,
        consume_drafts_named: Optional[str] = None,
        draft_code: Optional[str] = None,
    ) -> int:
        doc_ref = self._col.document(f"{user_id}:{skill.name}")
        user_query = self._user_query(user_id)
        drafts_query = self._drafts_named_query(user_id, consume_drafts_named) if consume_drafts_named else None
        draft_ref = self._drafts.document(f"{user_id}:{draft_code}") if draft_code else None
        manifest_shas = {f.sha256 for f in skill.files}
        transaction = self._db.transaction()

        @firestore.async_transactional
        async def _txn(txn) -> int:
            # ---- Reads: all of them before any write (Firestore transaction rule). ----
            snapshot = await doc_ref.get(transaction=txn)
            if snapshot.exists:
                version = int(snapshot.to_dict()["current"]) + 1
            else:
                existing = await user_query.get(transaction=txn)
                if len(existing) >= cap:
                    raise SkillCapExceeded(f"user already has {len(existing)} skills (cap {cap})")
                version = 1
            drafts = await drafts_query.get(transaction=txn) if drafts_query is not None else []

            staged_contents: Dict[str, str] = {}
            if draft_ref is not None:
                draft_snap = await draft_ref.get(transaction=txn)
                if not draft_snap.exists:
                    # A concurrent `$skill save` of the same code consumed it.
                    raise SkillDraftNotFound(f"draft {draft_code} no longer exists")
                staged = list((draft_snap.to_dict() or {}).get("staged") or [])
                if staged:
                    refs = [draft_ref.collection(_DRAFT_FILES).document(s) for s in staged]
                    async for snap in self._db.get_all(refs, transaction=txn):
                        if not snap.exists:
                            logger.warning("⚠️ [Skills] Staged file missing for draft %s…:%s",
                                           user_id[:8], draft_code)
                            raise SkillFileMissing("draft files expired; draft again")
                        staged_contents[snap.id] = (snap.to_dict() or {})["content"]
                    if len(staged_contents) < len(set(staged)):
                        raise SkillFileMissing("draft files expired; draft again")

            inherited = sorted(manifest_shas - set(staged_contents))
            if inherited:
                refs = [doc_ref.collection(_FILES).document(s) for s in inherited]
                present = set()
                async for snap in self._db.get_all(refs, field_paths=["size"], transaction=txn):
                    if snap.exists:
                        present.add(snap.id)
                missing = [s for s in inherited if s not in present]
                if missing:
                    # The message reaches the owner: name the path, never the hash.
                    paths = sorted(f.path for f in skill.files if f.sha256 == missing[0])
                    logger.warning("⚠️ [Skills] Inherited file %s missing for %s…:%s",
                                   ", ".join(paths), user_id[:8], skill.name)
                    raise SkillFileMissing(
                        f"file {', '.join(paths)} of {skill.name} no longer exists; draft again"
                    )

            # ---- Writes. ----
            now = datetime.now(timezone.utc)
            for sha, content in staged_contents.items():
                # Never `expires_at` here: a skill's own files must not be covered by TTL.
                txn.set(doc_ref.collection(_FILES).document(sha), {
                    "content": content,
                    "size": len(content.encode("utf-8")),
                    "created_at": now,
                })
            manifest = _manifest(skill)
            txn.set(doc_ref.collection(_VERSIONS).document(f"v{version}"), {
                "name": skill.name,
                "description": skill.description,
                "body": skill.body,
                "files": manifest,
                "saved_at": now,
            })
            txn.set(doc_ref, {
                "user_id": user_id,
                "account_id": account_id,
                "name": skill.name,
                "description": skill.description,
                "body": skill.body,
                "files": manifest,
                "current": version,
                "updated_at": now,
            })
            for d in drafts:
                # Addressed from the draft's `staged` list: no extra reads.
                for s in (d.to_dict() or {}).get("staged") or []:
                    txn.delete(d.reference.collection(_DRAFT_FILES).document(s))
                txn.delete(d.reference)
            return version

        return await _txn(transaction)

    async def create_draft(
        self, user_id: str, code: str, skill: Skill, staged: Optional[Mapping[str, str]] = None,
    ) -> bool:
        staged = staged or {}
        now = datetime.now(timezone.utc)
        expires = now + DRAFT_TTL
        draft_ref = self._drafts.document(f"{user_id}:{code}")
        # Files first (written concurrently), then the draft doc: a valid code never points at
        # missing files. On a code collision the staged docs land in the existing draft's
        # `draft_files`, which is harmless: they are content-addressed and expire by TTL.
        await asyncio.gather(*(
            draft_ref.collection(_DRAFT_FILES).document(sha).set({
                "content": content,
                "size": len(content.encode("utf-8")),
                "created_at": now,
                "expires_at": expires,
            })
            for sha, content in staged.items()
        ))
        try:
            await draft_ref.create({
                "user_id": user_id,
                "name": skill.name,
                "description": skill.description,
                "body": skill.body,
                "files": _manifest(skill),
                "staged": sorted(staged),
                "created_at": now,
                "expires_at": expires,
            })
            return True
        except AlreadyExists:
            return False

    async def get_draft(self, user_id: str, code: str) -> Optional[Skill]:
        snapshot = await self._drafts.document(f"{user_id}:{code}").get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        # TTL deletion lags up to a day; an expired draft is gone as far as callers are concerned.
        if _expired(data, datetime.now(timezone.utc)):
            return None
        try:
            return _skill_from(data)
        except (KeyError, ValidationError) as e:
            logger.error("❌ [Skills] Skipping corrupt draft doc %s…:%s: %s", user_id[:8], code, e)
            return None

    async def get_draft_files(self, user_id: str, code: str) -> Dict[str, str]:
        draft_ref = self._drafts.document(f"{user_id}:{code}")
        snapshot = await draft_ref.get()
        if not snapshot.exists:
            raise SkillDraftNotFound(f"draft {code} no longer exists")
        staged = list((snapshot.to_dict() or {}).get("staged") or [])
        if not staged:
            return {}
        refs = [draft_ref.collection(_DRAFT_FILES).document(s) for s in staged]
        contents: Dict[str, str] = {}
        async for snap in self._db.get_all(refs):
            if snap.exists:
                contents[snap.id] = (snap.to_dict() or {}).get("content", "")
        # TTL deletion is not synchronized across documents: some staged files may be gone.
        if len(contents) < len(set(staged)):
            logger.warning("⚠️ [Skills] Draft %s…:%s has %d of %d staged files",
                           user_id[:8], code, len(contents), len(set(staged)))
            raise SkillFileMissing("draft files expired; draft again")
        return contents

    async def delete_skill(self, user_id: str, name: str) -> bool:
        doc_ref = self._col.document(f"{user_id}:{name}")
        snapshot = await doc_ref.get()
        if not snapshot.exists:
            return False
        # Refs only (no content reads), gathered before the versions query.
        refs = [ref async for ref in doc_ref.collection(_FILES).list_documents()]
        for version_doc in await doc_ref.collection(_VERSIONS).get():
            refs.append(version_doc.reference)
        # Index doc last, so it lands in the final chunk: a failure midway leaves it in place
        # and a retried delete finishes the job.
        refs.append(doc_ref)
        for start in range(0, len(refs), _DELETE_CHUNK):
            batch = self._db.batch()
            for ref in refs[start:start + _DELETE_CHUNK]:
                batch.delete(ref)
            await batch.commit()
        return True

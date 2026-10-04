"""Reads and saves skills — system (git, shared) and custom (per-user, Firestore).

Saving rejects flagged text — a skill is followed verbatim. Custom skills are drafted
by the model (`draft`) before the owner confirms them by code (`save_draft`), or saved
directly by the owner (`save`). A custom skill can shadow a system skill of the same
name, but a *new* custom skill cannot claim a system name (`SkillNameReserved`).
"""

import secrets
from typing import List, Sequence, Tuple

from ..domain.exceptions import SkillCapExceeded, SkillDraftNotFound, SkillNameReserved, SkillRejected
from ..domain.prompt_v3.security import TrustZone
from ..domain.skill import MAX_CUSTOM_SKILLS_PER_USER, Skill
from ..ports.security_port import SecurityPort
from ..ports.skill_repository import SkillRepository
from ..utils.logger import logger

_MAX_DRAFT_CODE_ATTEMPTS = 5


class SkillService:

    def __init__(
        self,
        repository: SkillRepository,
        security_port: SecurityPort,
        system_skills: Sequence[Skill] = (),
    ):
        self._repo = repository
        self._security = security_port
        self._system_skills = {s.name: s for s in system_skills}

    def is_system(self, name: str) -> bool:
        return name in self._system_skills

    async def list_skills(self, user_id: str) -> List[Skill]:
        try:
            custom = await self._repo.list_current(user_id)
        except Exception as e:
            logger.error("❌ [Skills] list_current failed for user %s, falling back to system skills: %s",
                         user_id[:8], e)
            custom = []
        merged = dict(self._system_skills)
        for skill in custom:
            if skill.name in self._system_skills:
                logger.warning("⚠️ [Skills] Custom skill %s shadows a system skill for user %s",
                               skill.name, user_id[:8])
            merged[skill.name] = skill
        return sorted(merged.values(), key=lambda s: s.name)

    async def list_owned(self, user_id: str) -> Tuple[List[Skill], List[Skill]]:
        custom = await self._repo.list_current(user_id)
        system = sorted(self._system_skills.values(), key=lambda s: s.name)
        return custom, system

    async def _check(self, user_id: str, skill: Skill) -> None:
        if self.is_system(skill.name):
            raise SkillNameReserved(f"'{skill.name}' is a system skill name")
        for field, text in (("description", skill.description), ("body", skill.body)):
            try:
                result = await self._security.validate(
                    text, context=f"skill_{field}_user_{user_id}", zone=TrustZone.UNTRUSTED,
                )
            except ValueError as e:  # the regex/composite adapters raise on HIGH/CRITICAL
                logger.warning("🛡️ [Skills] Rejected skill %s: %s blocked (%s)", skill.name, field, e)
                raise SkillRejected(f"{field} blocked by the security check: {e}") from e
            if result.action_taken != "passed":
                logger.warning(
                    "🛡️ [Skills] Rejected skill %s: %s flagged %s",
                    skill.name, field, result.patterns_detected,
                )
                raise SkillRejected(f"{field} flagged by the security check: {result.patterns_detected}")

    async def save(self, user_id: str, account_id: str, skill: Skill) -> int:
        await self._check(user_id, skill)
        version = await self._repo.save_version(user_id, account_id, skill, cap=MAX_CUSTOM_SKILLS_PER_USER)
        logger.info("💾 [Skills] Saved %s v%s for user %s", skill.name, version, user_id[:8])
        return version

    async def draft(self, user_id: str, skill: Skill) -> str:
        await self._check(user_id, skill)
        custom = await self._repo.list_current(user_id)
        if skill.name not in {s.name for s in custom} and len(custom) >= MAX_CUSTOM_SKILLS_PER_USER:
            raise SkillCapExceeded(f"user already has {len(custom)} skills (cap {MAX_CUSTOM_SKILLS_PER_USER})")
        for _ in range(_MAX_DRAFT_CODE_ATTEMPTS):
            code = secrets.token_hex(2)
            if await self._repo.create_draft(user_id, code, skill):
                logger.info("📝 [Skills] Drafted %s for user %s", skill.name, user_id[:8])
                return code
        logger.error("❌ [Skills] Could not allocate a unique draft code for user %s after %d attempts",
                     user_id[:8], _MAX_DRAFT_CODE_ATTEMPTS)
        raise RuntimeError(f"could not allocate a draft code after {_MAX_DRAFT_CODE_ATTEMPTS} attempts")

    async def save_draft(self, user_id: str, account_id: str, code: str) -> Tuple[str, int]:
        skill = await self._repo.get_draft(user_id, code)
        if skill is None:
            raise SkillDraftNotFound(f"no pending draft {code!r} for user {user_id[:8]}")
        await self._check(user_id, skill)
        version = await self._repo.save_version(
            user_id, account_id, skill, cap=MAX_CUSTOM_SKILLS_PER_USER, consume_drafts_named=skill.name,
        )
        logger.info("💾 [Skills] Saved %s v%s for user %s (from draft)", skill.name, version, user_id[:8])
        return skill.name, version

    async def delete(self, user_id: str, name: str) -> bool:
        deleted = await self._repo.delete_skill(user_id, name)
        if not deleted and self.is_system(name):
            raise SkillNameReserved(f"'{name}' is a system skill and cannot be deleted")
        return deleted

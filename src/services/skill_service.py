"""Reads and saves custom skills. Saving rejects flagged text — a skill is followed verbatim."""

from typing import List

from ..domain.exceptions import SkillRejected
from ..domain.prompt_v3.security import TrustZone
from ..domain.skill import MAX_CUSTOM_SKILLS_PER_USER, Skill
from ..ports.security_port import SecurityPort
from ..ports.skill_repository import SkillRepository
from ..utils.logger import logger


class SkillService:

    def __init__(self, repository: SkillRepository, security_port: SecurityPort):
        self._repo = repository
        self._security = security_port

    async def list_skills(self, user_id: str) -> List[Skill]:
        return await self._repo.list_current(user_id)

    async def save(self, user_id: str, account_id: str, skill: Skill) -> int:
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
        version = await self._repo.save_version(user_id, account_id, skill, cap=MAX_CUSTOM_SKILLS_PER_USER)
        logger.info("💾 [Skills] Saved %s v%s for user %s", skill.name, version, user_id[:8])
        return version

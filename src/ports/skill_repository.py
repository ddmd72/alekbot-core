"""Storage of custom (per-user) skills."""

from abc import ABC, abstractmethod
from typing import List

from ..domain.skill import Skill


class SkillRepository(ABC):

    @abstractmethod
    async def list_current(self, user_id: str) -> List[Skill]:
        """The user's skills at their current version, sorted by name. Corrupt entries are skipped."""

    @abstractmethod
    async def save_version(self, user_id: str, account_id: str, skill: Skill, cap: int) -> int:
        """Write the next version and make it current, atomically. Returns the new version.

        Raises SkillCapExceeded when the skill is new and the user already has `cap` skills.
        """

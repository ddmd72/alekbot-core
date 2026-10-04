"""Storage of custom (per-user) skills."""

from abc import ABC, abstractmethod
from typing import List, Optional

from ..domain.skill import Skill


class SkillRepository(ABC):

    @abstractmethod
    async def list_current(self, user_id: str) -> List[Skill]:
        """The user's skills at their current version, sorted by name. Corrupt entries are skipped."""

    @abstractmethod
    async def save_version(
        self, user_id: str, account_id: str, skill: Skill, cap: int,
        consume_drafts_named: Optional[str] = None,
    ) -> int:
        """Write the next version and make it current, atomically. Returns the new version.

        When `consume_drafts_named` is set, deletes all drafts with that name for this user
        in the same transaction. Raises SkillCapExceeded when the skill is new and the user
        already has `cap` skills.
        """

    @abstractmethod
    async def create_draft(self, user_id: str, code: str, skill: Skill) -> bool:
        """Create a draft keyed by `{user_id}:{code}`. Returns False if that code already exists."""

    @abstractmethod
    async def get_draft(self, user_id: str, code: str) -> Optional[Skill]:
        """Fetch a draft by `{user_id}:{code}`. Returns None if absent or corrupt."""

    @abstractmethod
    async def delete_skill(self, user_id: str, name: str) -> bool:
        """Delete all versions and the index doc for a skill. Returns False if it doesn't exist."""

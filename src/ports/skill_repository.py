"""Storage of custom (per-user) skills."""

from abc import ABC, abstractmethod
from typing import Dict, List, Mapping, Optional

from ..domain.skill import Skill


class SkillRepository(ABC):

    @abstractmethod
    async def list_current(self, user_id: str) -> List[Skill]:
        """The user's skills at their current version (with file manifests), sorted by name.
        Corrupt entries are skipped."""

    @abstractmethod
    async def get_current(self, user_id: str, name: str) -> Optional[Skill]:
        """One skill at its current version, with its file manifest. None if absent or corrupt."""

    @abstractmethod
    async def get_file(self, user_id: str, name: str, sha256: str) -> Optional[str]:
        """Content of one of the skill's stored files, by hash. None if absent."""

    @abstractmethod
    async def save_version(
        self, user_id: str, account_id: str, skill: Skill, cap: int,
        consume_drafts_named: Optional[str] = None,
        draft_code: Optional[str] = None,
    ) -> int:
        """Write the next version and make it current, atomically. Returns the new version.

        The version and index docs carry `skill.files` as the manifest. When `draft_code` is
        set, that draft's staged files are copied into the skill's own files in the same
        transaction. Every other manifest hash must already be stored for the skill.

        When `consume_drafts_named` is set, deletes all drafts with that name for this user,
        with their staged files, in the same transaction.

        Raises SkillCapExceeded when the skill is new and the user already has `cap` skills,
        SkillDraftNotFound when `draft_code`'s draft no longer exists, and SkillFileMissing
        when a staged or inherited file is no longer stored.
        """

    @abstractmethod
    async def create_draft(
        self, user_id: str, code: str, skill: Skill, staged: Optional[Mapping[str, str]] = None,
    ) -> bool:
        """Create a draft keyed by `{user_id}:{code}`. Returns False if that code already exists.

        `staged` maps sha256 → content for files the skill does not already hold; they are
        stored with the draft (written before the draft doc) and copied on save.
        """

    @abstractmethod
    async def get_draft(self, user_id: str, code: str) -> Optional[Skill]:
        """Fetch a draft by `{user_id}:{code}`, with its manifest. Returns None if absent,
        expired or corrupt."""

    @abstractmethod
    async def get_draft_files(self, user_id: str, code: str) -> Dict[str, str]:
        """The draft's staged files, sha256 → content. Raises SkillFileMissing when any staged
        file is gone, SkillDraftNotFound when the draft itself is gone."""

    @abstractmethod
    async def delete_skill(self, user_id: str, name: str) -> bool:
        """Delete all versions, files and the index doc for a skill. Returns False if it doesn't exist."""

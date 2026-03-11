"""
EmailExclusionsPort — manage sender/domain/subject exclusion patterns.
Applied as a fast pre-filter before LLM classification.
See docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md §2.1.2.
"""

from abc import ABC, abstractmethod
from typing import List

from src.domain.email import EmailExclusion


class EmailExclusionsPort(ABC):

    @abstractmethod
    async def get_exclusions(self, user_id: str) -> List[EmailExclusion]:
        """
        Load all active exclusion patterns for user.
        Called once per indexing job as a fast pre-filter before LLM.
        """

    @abstractmethod
    async def add_exclusions(self, exclusions: List[EmailExclusion]) -> None:
        """
        Persist auto-detected patterns.
        Called when classifier identifies recurring low-value senders.
        Idempotent: no-op if identical pattern already exists.
        """

    @abstractmethod
    async def delete_exclusion(self, user_id: str, exclusion_id: str) -> None:
        """User removes a pattern via Cabinet UI."""

    @abstractmethod
    async def list_exclusions(self, user_id: str) -> List[EmailExclusion]:
        """
        For Cabinet display — returns all patterns with reason and created_at.
        Semantically distinct from get_exclusions (display vs. filtering),
        backed by the same underlying query.
        """

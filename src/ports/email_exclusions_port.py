"""
EmailExclusionsPort — manage sender/domain/subject exclusion patterns.
Applied as a fast pre-filter before LLM classification.
See docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md §2.1.2.
"""

from abc import ABC, abstractmethod
from typing import List

from src.domain.email import EmailExclusion


class EmailExclusionsPort(ABC):
    """Sender/domain/subject patterns skipped before classification. Backend errors propagate.

    Only `get_exclusions` has a caller in src/ (nothing writes the patterns today).
    """

    @abstractmethod
    async def get_exclusions(self, user_id: str) -> List[EmailExclusion]:
        """
        Load all active exclusion patterns for user.
        Called once per indexing job as a fast pre-filter before LLM.

        Tenancy: none of another user's patterns. Order: none. Legacy: a pattern missing a field is skipped (logged).
        """

    @abstractmethod
    async def add_exclusions(self, exclusions: List[EmailExclusion]) -> None:
        """
        Persist auto-detected patterns.
        Called when classifier identifies recurring low-value senders.
        No caller in src/.

        Idempotency: one pattern per (user, type, pattern); a repeat replaces it (the reason and created_at of the
        newer call win). Atomicity: all or none.
        """

    @abstractmethod
    async def delete_exclusion(self, user_id: str, exclusion_id: str) -> None:
        """User removes a pattern via Cabinet UI. No caller in src/.

        Tenancy: not checked: the exclusion id alone selects the pattern, so a caller must check ownership first. A
        missing id is a no-op.
        """

    @abstractmethod
    async def list_exclusions(self, user_id: str) -> List[EmailExclusion]:
        """
        For Cabinet display — returns all patterns with reason and created_at.
        Semantically distinct from get_exclusions (display vs. filtering),
        backed by the same underlying query. No caller in src/.
        """

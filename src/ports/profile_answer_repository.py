from abc import ABC, abstractmethod

from ..domain.profile_answers import OwnerAnswer, ProfileAnswers


class ProfileAnswerRepository(ABC):
    """The owner's answers to profile questions (docs/10_rfcs/FACT_PROFILE_RFC.md §4.7). Written only from the owner's
    answer; never by the profiler, the consolidator or the review pass. Read whole by the profile step.

    Tenancy: the document id is the account id; one account never reads or records into another's answers. Backend
    errors propagate.
    """

    @abstractmethod
    async def get(self, account_id: str) -> ProfileAnswers:
        """The account's answers; `EMPTY_ANSWERS` when it has none. A stored document that does not parse, or that names
        another account, raises ValueError (never empty: deriving without the answers would undo them)."""

    @abstractmethod
    async def record(self, account_id: str, answer: OwnerAnswer) -> bool:
        """Add `answer` in one transaction (read, add, set the whole document with the time of the write). False, and
        nothing written, when its key already has an answer: the first answer for a key wins."""

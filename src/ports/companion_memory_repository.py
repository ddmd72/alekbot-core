"""
CompanionMemoryRepository — port for the session-scoped companion memory
store. One collection, filtered by session_id AND account_id (the latter
as a tenancy guard — see find_nearest's docstring below).

RFC: docs/10_rfcs/COMPANION_AGENTS_RFC.md §6. Deliberately NOT FactRepository
— see RFC §11 "Rejected: Reusing FactEntity/FactRepository for companion
records" (different identity model: session-keyed, not account/user-keyed).

Single implementation today (FirestoreCompanionMemoryRepository); this is a
system-boundary port (Firestore vector search), not a substitution port —
justified per root CLAUDE.md's "system boundary" criterion, not "2+ impls".
"""
from abc import ABC, abstractmethod
from typing import List

from ..domain.companion import CompanionRecord


class CompanionMemoryRepository(ABC):

    @abstractmethod
    async def save_batch(self, records: List[CompanionRecord]) -> int:
        """Persist records. Returns count written."""

    @abstractmethod
    async def find_nearest(
        self,
        session_id: str,
        account_id: str,
        query_vector: List[float],
        limit: int = 10,
    ) -> List[CompanionRecord]:
        """Vector search scoped to one session AND one account. Must filter by
        BOTH session_id and account_id — session_id alone is not a sufficient
        tenancy guarantee (two accounts on the same platform are not provably
        collision-free on channel-derived session keys)."""

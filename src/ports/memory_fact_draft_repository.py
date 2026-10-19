from abc import ABC, abstractmethod
from typing import Optional

from ..domain.memory_fact import MemoryFactDraft


class MemoryFactDraftRepository(ABC):
    """Proposed memory facts awaiting the owner's `$memory save` (MEMORY_FACT_CONFIRMATION_RFC)."""

    @abstractmethod
    async def create_draft(self, user_id: str, code: str, draft: MemoryFactDraft) -> bool:
        """Create a draft keyed by `{user_id}:{code}`. Returns False if that code already exists."""

    @abstractmethod
    async def get_draft(self, user_id: str, code: str) -> Optional[MemoryFactDraft]:
        """Fetch a draft. Returns None if absent, expired or corrupt."""

    @abstractmethod
    async def delete_draft(self, user_id: str, code: str) -> None:
        """Delete a draft; a missing draft is not an error."""

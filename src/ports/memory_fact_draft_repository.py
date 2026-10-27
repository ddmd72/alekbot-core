from abc import ABC, abstractmethod
from typing import Optional

from ..domain.memory_fact import MemoryFactDraft


class MemoryFactDraftRepository(ABC):
    """Proposed memory facts awaiting the owner's `$memory save` (MEMORY_FACT_CONFIRMATION_RFC).

    Tenancy: the user_id is part of the key; another user's code is absent. Backend errors propagate.
    """

    @abstractmethod
    async def create_draft(self, user_id: str, code: str, draft: MemoryFactDraft) -> bool:
        """Create a draft keyed by (user_id, code); it expires 7 days after creation.

        Returns False when that code exists for the user; the stored draft is then unchanged.
        """

    @abstractmethod
    async def get_draft(self, user_id: str, code: str) -> Optional[MemoryFactDraft]:
        """Fetch a draft. Returns None if absent, expired or corrupt.

        Expiry: a draft whose expiry has passed reads as None whether or not the backend has removed it yet; a naive
        expiry is UTC. A draft with no expiry: no writer produces one; behaviour pending the data audit. An
        origin_session_id of None reads None.
        """

    @abstractmethod
    async def delete_draft(self, user_id: str, code: str) -> None:
        """Delete a draft; a missing draft is not an error.

        There is no consume-once read: two concurrent readers both see the draft until one deletes it.
        """

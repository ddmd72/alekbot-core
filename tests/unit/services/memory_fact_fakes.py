"""In-memory MemoryFactDraftRepository for service and handler tests."""

from typing import Dict, Optional, Tuple

from src.domain.memory_fact import MemoryFactDraft
from src.ports.memory_fact_draft_repository import MemoryFactDraftRepository


class InMemoryMemoryFactDraftRepository(MemoryFactDraftRepository):

    def __init__(self) -> None:
        self.drafts: Dict[Tuple[str, str], MemoryFactDraft] = {}

    async def create_draft(self, user_id: str, code: str, draft: MemoryFactDraft) -> bool:
        if (user_id, code) in self.drafts:
            return False
        self.drafts[(user_id, code)] = draft
        return True

    async def get_draft(self, user_id: str, code: str) -> Optional[MemoryFactDraft]:
        return self.drafts.get((user_id, code))

    async def delete_draft(self, user_id: str, code: str) -> None:
        self.drafts.pop((user_id, code), None)


class InMemorySessionStore:
    """Records appended messages per session; `fail` makes the next append raise."""

    def __init__(self) -> None:
        self.sessions: Dict[str, list] = {}
        self.fail = False

    async def append_messages_batch(self, session_id, messages, owner_id=None, *, raise_on_error=False):
        if self.fail:
            if raise_on_error:
                raise RuntimeError("append failed")
            return
        self.sessions.setdefault(session_id, []).extend(messages)

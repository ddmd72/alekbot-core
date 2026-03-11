"""
AgentNotePort — abstract interface for orchestrator note storage.

Port justification: Firestore adapter today; future alternatives possible
(Redis for ephemeral notes, encrypted storage, external task system).
Port methods carry user_id explicitly — no auth artifacts at port boundary.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import List

from ..domain.agent_note import AgentNote, NoteCreate, NoteUpdate


class AgentNotePort(ABC):

    @abstractmethod
    async def create_note(self, data: NoteCreate) -> AgentNote:
        """Create a new note. Returns the created note with generated note_id."""

    @abstractmethod
    async def delete_note(self, note_id: str, user_id: str) -> bool:
        """Delete note by ID. Returns True if deleted, False if not found."""

    @abstractmethod
    async def update_note(self, data: NoteUpdate) -> AgentNote:
        """Update an existing note. Returns updated note."""

    @abstractmethod
    async def list_active_notes(self, user_id: str, as_of: datetime) -> List[AgentNote]:
        """
        Return all notes currently active for the user:
          (visible_after IS NULL OR visible_after <= as_of)
          AND
          (expires_after IS NULL OR expires_after > as_of)
        Ordered by created_at ASC (oldest first → stable order in prompt).
        """

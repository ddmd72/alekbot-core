"""
AgentNotePort — abstract interface for proactive self-reminder storage.

Port justification: Firestore adapter today; future alternatives possible
(encrypted storage, external task system).
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
        Return all notes for the user where due > as_of (not yet fired).
        Ordered by created_at ASC (oldest first → stable order in prompt).
        """

    @abstractmethod
    async def list_due_reminders(self, as_of: datetime) -> List[AgentNote]:
        """
        Cross-user scan: return all notes with due <= as_of.
        Called by fire_due_reminders worker only.
        """

    @abstractmethod
    async def reschedule(self, note_id: str, next_due: datetime, last_fired: datetime) -> None:
        """
        Update due to next_due and set last_fired.
        Called after firing a recurrent reminder.
        No user_id ownership check — cron owns the lock.
        """

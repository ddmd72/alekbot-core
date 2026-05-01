"""
AgentNotePort — abstract interface for proactive self-reminder storage.

Port justification: Firestore adapter today; future alternatives possible
(encrypted storage, external task system).
Port methods carry user_id explicitly — no auth artifacts at port boundary.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional

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
    async def get_note(self, user_id: str, note_id: str) -> Optional[AgentNote]:
        """Fetch a single note by id, scoped by user_id ownership.

        Returns ``None`` if the note does not exist OR if it belongs to
        a different user. Used by the reminder execute-worker to load
        the note pointed to by a fire-payload.
        """

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
    async def reschedule_if_due_at(
        self,
        note_id: str,
        expected_due: datetime,
        next_due: datetime,
        last_fired: datetime,
    ) -> bool:
        """Atomically reschedule the note ONLY IF its current ``due``
        equals ``expected_due``.

        Returns ``True`` if rescheduled (this caller owns this fire);
        ``False`` if ``due`` has already moved (another cron tick handled
        it). Caller MUST treat False as "skip — someone else owns it".

        Idempotency primitive — replaces the unconditional ``reschedule``
        method. Implemented via Firestore transaction with read-modify-write
        precondition on ``due``.

        See: docs/10_rfcs/NOTIFICATION_DELIVERY_REFACTOR_RFC.md § 7 D.2.
        """

    @abstractmethod
    async def delete_if_due_at(
        self,
        note_id: str,
        user_id: str,
        expected_due: datetime,
    ) -> bool:
        """One-time variant: atomically delete the note ONLY IF its
        current ``due`` equals ``expected_due`` AND it belongs to
        ``user_id``.

        Returns ``True`` if deleted (this caller owns this fire);
        ``False`` if ``due`` already moved or ownership mismatched.

        Same idempotency contract as ``reschedule_if_due_at``, but for
        non-recurrent reminders that are removed after firing.
        """

    @abstractmethod
    async def mark_fire_delivered(self, note_id: str, due_at: datetime) -> None:
        """Record that the fire scheduled for ``due_at`` has been
        delivered to the user.

        Idempotency token consumed by the reminder execute-worker to
        short-circuit duplicate Cloud Tasks deliveries (compares
        ``last_delivered_due == due_at`` before invoking ``notify``).

        Idempotent: calling twice with the same ``due_at`` is safe.

        See: docs/10_rfcs/NOTIFICATION_DELIVERY_REFACTOR_RFC.md § 7 D.3.
        """

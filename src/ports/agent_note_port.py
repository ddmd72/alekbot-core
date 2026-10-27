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
    """Self-reminders. Holds for every method:

    - Tenancy: a note belongs to `user_id`. Every user-facing method (get_note, update_note, delete_note,
      delete_if_due_at, claim_one_time_if_due_at, list_active_notes) answers a foreign note as absent and changes
      nothing. list_due_reminders and reschedule_if_due_at are cross-user (the fire tick only).
    - Ids: a note id is unique across all users and never reused for another note (P11-G1-2: today two notes created in
      the same millisecond share one id and the second replaces the first).
    - Times: every datetime read back from the store is aware UTC.
    - Legacy: a note without `instruction` reads its `text`; a pre-2026-07-30 map recurrence {type, interval} reads as
      an RRULE; an unreadable recurrence reads as one-time; an unknown complexity reads as None; a missing
      workspace_file reads as None. A note missing user_id, text or due, or whose due is not a timestamp: no writer
      produces one; behaviour pending the data audit.
    - Errors: backend errors propagate.
    """

    @abstractmethod
    async def create_note(self, data: NoteCreate) -> AgentNote:
        """Store a new note for data.user_id with created_at = now, last_fired None. Returns the note; callers rely
        on its `note_id` only (the returned `complexity` is not populated).

        Errors: ValueError, nothing written, when text exceeds 25 words or the user already has 30 active notes
        (due > now). Atomicity: the cap is checked by a read before the write; concurrent creates can pass it together.
        """

    @abstractmethod
    async def delete_note(self, note_id: str, user_id: str) -> bool:
        """Delete the user's note. True if deleted; False if absent or foreign. ValueError on an empty id."""

    @abstractmethod
    async def update_note(self, data: NoteUpdate) -> AgentNote:
        """PATCH the user's note: only the fields given change; clear_recurrence and clear_workspace_file set them
        to None. Returns the note as stored after the update.

        Errors: ValueError, nothing written, when the note is absent or foreign, the id is empty, or text exceeds 25
        words. Atomicity: read then update, no precondition; a note deleted between them raises the backend's not-found error (NotFound today).
        """

    @abstractmethod
    async def get_note(self, user_id: str, note_id: str) -> Optional[AgentNote]:
        """Fetch a single note by id, scoped by user_id ownership.

        Returns ``None`` if the note does not exist, if it belongs to
        a different user, or if the id is empty. Used by the reminder
        execute-worker to load the note pointed to by a fire-payload.
        """

    @abstractmethod
    async def list_active_notes(self, user_id: str, as_of: datetime) -> List[AgentNote]:
        """
        Return all notes for the user where due > as_of (not yet fired).
        Ordered by created_at ASC (oldest first → stable order in prompt).
        No limit (the 30-note cap bounds it). An `as_of` before every due (the
        epoch) returns every note of the user, including fired one-time notes
        awaiting deletion.
        """

    @abstractmethod
    async def list_due_reminders(self, as_of: datetime) -> List[AgentNote]:
        """
        Cross-user scan: return all notes with due <= as_of.
        Called by fire_due_reminders worker only. Includes one-time notes
        already claimed (last_fired >= due) until the worker deletes them.
        Order: none promised. No limit.
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
        method.

        No ownership check. Absent note or empty id: False. Of concurrent
        callers with the same ``expected_due`` exactly one gets True.

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
        Observed contract; no caller in src/.
        """

    @abstractmethod
    async def claim_one_time_if_due_at(
        self,
        note_id: str,
        user_id: str,
        expected_due: datetime,
        last_fired: datetime,
    ) -> bool:
        """One-time claim that marks the fire WITHOUT deleting the note.

        Atomically stamps ``last_fired`` ONLY IF the note still belongs to
        ``user_id``, its ``due`` equals ``expected_due``, AND it has not
        already fired for this ``due`` (``last_fired`` is None or earlier
        than ``due``). Returns ``True`` if this caller won the claim,
        ``False`` otherwise (ownership mismatch, ``due`` moved, or a
        concurrent tick already claimed it).

        Unlike the recurrent ``reschedule_if_due_at``, ``due`` does not move,
        so the ``last_fired < due`` guard is what makes the claim
        at-most-once: after the first claim ``last_fired >= due`` and every
        later tick fails the precondition.

        The note is preserved so the execute-worker can load its content to
        build the alert (the one-time note is deleted by the worker AFTER
        successful delivery, mirroring the recurrent flow). This replaces the
        old delete-at-claim path, which removed the note before the worker
        could read it.

        Absent note or empty id: False. Of concurrent callers exactly one
        gets True.
        """

    @abstractmethod
    async def mark_fire_delivered(self, note_id: str, due_at: datetime) -> None:
        """Record that the fire scheduled for ``due_at`` has been
        delivered to the user.

        Idempotency token consumed by the reminder execute-worker to
        short-circuit duplicate Cloud Tasks deliveries (compares
        ``last_delivered_due == due_at`` before invoking ``notify``).

        Idempotent: calling twice with the same ``due_at`` is safe.
        Errors: an absent note raises the backend's not-found error (NotFound today) (the execute worker's retry
        then finds the note gone and acks). An empty id is a no-op. No
        ownership check.

        See: docs/10_rfcs/NOTIFICATION_DELIVERY_REFACTOR_RFC.md § 7 D.3.
        """

"""
FirestoreAgentNoteAdapter — persists orchestrator self-reminders.

Collection: {env_prefix}orchestrator_notes
Document ID: epoch milliseconds string (time-sortable, collision window = 1ms)

Constraints enforced here (not at port boundary):
  - MAX_WORDS_PER_NOTE = 25  (text label only — instruction has no limit)
  - MAX_NOTES_PER_USER = 30

Required Firestore index:
  Collection: orchestrator_notes
  Field: due ASC
  (enables list_due_reminders WHERE due <= :now without full collection scan)

Migration note: existing documents may have visible_after/expires_after fields.
These are silently ignored — _dict_to_note does not map them.
Existing documents without 'instruction' fall back to 'text'.
"""

from datetime import datetime, timezone
from typing import List, Optional

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from ..config.environment import EnvironmentConfig
from ..domain.agent_note import AgentNote, NoteCreate, NoteUpdate, ReminderRecurrence
from ..domain.task_complexity import TaskComplexity
from ..ports.agent_note_port import AgentNotePort
from ..utils.logger import logger


class FirestoreAgentNoteAdapter(AgentNotePort):

    MAX_NOTES_PER_USER = 30
    MAX_WORDS_PER_NOTE = 25

    def __init__(self, db_client, env_config: EnvironmentConfig):
        self._db = db_client
        self._col = self._db.collection(env_config.orchestrator_notes_collection)
        logger.info(
            "📝 AgentNote repository initialized: %s",
            env_config.orchestrator_notes_collection,
        )

    # ------------------------------------------------------------------
    # Port implementation
    # ------------------------------------------------------------------

    async def create_note(self, data: NoteCreate) -> AgentNote:
        word_count = len(data.text.split())
        if word_count > self.MAX_WORDS_PER_NOTE:
            raise ValueError(
                f"Note text exceeds {self.MAX_WORDS_PER_NOTE} words ({word_count})"
            )

        now = datetime.now(timezone.utc)
        active = await self.list_active_notes(data.user_id, as_of=now)
        if len(active) >= self.MAX_NOTES_PER_USER:
            raise ValueError(
                f"Note cap reached ({self.MAX_NOTES_PER_USER}). Delete a note first."
            )

        note_id = str(int(now.timestamp() * 1000))
        doc: dict = {
            "user_id": data.user_id,
            "text": data.text,
            "instruction": data.instruction,
            "created_at": now,
            "due": data.due,
            "last_fired": None,
        }
        if data.recurrence:
            doc["recurrence"] = {"type": data.recurrence.type, "interval": data.recurrence.interval}
        else:
            doc["recurrence"] = None
        if data.complexity:
            doc["complexity"] = data.complexity.value

        await self._col.document(note_id).set(doc)
        return AgentNote(
            note_id=note_id,
            user_id=data.user_id,
            text=data.text,
            instruction=data.instruction,
            created_at=now,
            due=data.due,
            recurrence=data.recurrence,
        )

    async def delete_note(self, note_id: str, user_id: str) -> bool:
        if not note_id:
            raise ValueError("delete_note: note_id must be a non-empty string")
        logger.debug("🗑️ [AgentNote] delete_note: note_id=%r user_id=%s", note_id, user_id[:8])
        doc_ref = self._col.document(note_id)
        doc = await doc_ref.get()
        if not doc.exists:
            return False
        data = doc.to_dict()
        if data.get("user_id") != user_id:
            logger.warning(
                "⚠️ [AgentNote] delete_note ownership mismatch: note=%s user=%s",
                note_id, user_id[:8],
            )
            return False
        await doc_ref.delete()
        return True

    async def update_note(self, data: NoteUpdate) -> AgentNote:
        if not data.note_id:
            raise ValueError("update_note: note_id must be a non-empty string")
        doc_ref = self._col.document(data.note_id)
        doc = await doc_ref.get()
        if not doc.exists:
            raise ValueError(f"Note not found: {data.note_id}")
        existing = doc.to_dict()
        if existing.get("user_id") != data.user_id:
            raise ValueError(
                f"Note {data.note_id} does not belong to user {data.user_id[:8]}"
            )

        updates: dict = {}
        if data.text is not None:
            word_count = len(data.text.split())
            if word_count > self.MAX_WORDS_PER_NOTE:
                raise ValueError(
                    f"Note text exceeds {self.MAX_WORDS_PER_NOTE} words ({word_count})"
                )
            updates["text"] = data.text
        if data.instruction is not None:
            updates["instruction"] = data.instruction
        if data.due is not None:
            updates["due"] = data.due
        if data.recurrence is not None:
            updates["recurrence"] = {"type": data.recurrence.type, "interval": data.recurrence.interval}
        if data.complexity is not None:
            updates["complexity"] = data.complexity.value

        if updates:
            await doc_ref.update(updates)

        merged = {**existing, **updates}
        return self._dict_to_note(data.note_id, merged)

    async def list_active_notes(self, user_id: str, as_of: datetime) -> List[AgentNote]:
        """Return notes that have not yet fired (due > as_of)."""
        docs = await self._col.where(filter=FieldFilter("user_id", "==", user_id)).get()
        result = []
        for doc in docs:
            note = self._dict_to_note(doc.id, doc.to_dict())
            if note.due <= as_of:
                continue
            result.append(note)
        return sorted(result, key=lambda n: n.created_at)

    async def list_due_reminders(self, as_of: datetime) -> List[AgentNote]:
        """Cross-user: all notes with due <= as_of. Requires Firestore index on due ASC."""
        docs = await self._col.where(filter=FieldFilter("due", "<=", as_of)).get()
        return [self._dict_to_note(doc.id, doc.to_dict()) for doc in docs]

    async def get_note(self, user_id: str, note_id: str) -> Optional[AgentNote]:
        if not note_id:
            return None
        doc = await self._col.document(note_id).get()
        if not doc.exists:
            return None
        data = doc.to_dict()
        if data.get("user_id") != user_id:
            logger.debug(
                "[AgentNote] get_note ownership mismatch: note=%s user=%s",
                note_id, user_id[:8],
            )
            return None
        return self._dict_to_note(note_id, data)

    async def reschedule_if_due_at(
        self,
        note_id: str,
        expected_due: datetime,
        next_due: datetime,
        last_fired: datetime,
    ) -> bool:
        """Atomic conditional reschedule via Firestore transaction.

        Reads the doc inside the transaction, compares ``due`` to
        ``expected_due`` (millisecond precision — Firestore stores
        timestamps with microsecond precision but native datetime
        equality works directly). Returns False if the doc is gone
        OR if ``due`` already moved.
        """
        if not note_id:
            return False
        doc_ref = self._col.document(note_id)
        transaction = self._db.transaction()

        @firestore.async_transactional
        async def _txn(txn) -> bool:
            snapshot = await doc_ref.get(transaction=txn)
            if not snapshot.exists:
                return False
            current_due = self._ensure_utc(snapshot.to_dict().get("due"))
            expected = self._ensure_utc(expected_due)
            if current_due != expected:
                return False
            txn.update(doc_ref, {"due": next_due, "last_fired": last_fired})
            return True

        return await _txn(transaction)

    async def delete_if_due_at(
        self,
        note_id: str,
        user_id: str,
        expected_due: datetime,
    ) -> bool:
        """Atomic conditional delete via Firestore transaction.

        Same precondition pattern as ``reschedule_if_due_at``, with the
        added ownership check (matching the unconditional ``delete_note``
        contract).
        """
        if not note_id:
            return False
        doc_ref = self._col.document(note_id)
        transaction = self._db.transaction()

        @firestore.async_transactional
        async def _txn(txn) -> bool:
            snapshot = await doc_ref.get(transaction=txn)
            if not snapshot.exists:
                return False
            data = snapshot.to_dict()
            if data.get("user_id") != user_id:
                logger.debug(
                    "[AgentNote] delete_if_due_at ownership mismatch: note=%s user=%s",
                    note_id, user_id[:8],
                )
                return False
            current_due = self._ensure_utc(data.get("due"))
            expected = self._ensure_utc(expected_due)
            if current_due != expected:
                return False
            txn.delete(doc_ref)
            return True

        return await _txn(transaction)

    async def claim_one_time_if_due_at(
        self,
        note_id: str,
        user_id: str,
        expected_due: datetime,
        last_fired: datetime,
    ) -> bool:
        """Atomic conditional one-time claim via Firestore transaction.

        Same precondition pattern as ``reschedule_if_due_at`` (ownership +
        ``due == expected_due``), with the extra ``last_fired < due`` guard.
        Because ``due`` is NOT moved here (one-time has no next due), that
        guard is the at-most-once primitive: after the first claim
        ``last_fired >= due``, so every later tick fails the precondition.
        The note is left in place for the execute-worker to read.
        """
        if not note_id:
            return False
        doc_ref = self._col.document(note_id)
        transaction = self._db.transaction()

        @firestore.async_transactional
        async def _txn(txn) -> bool:
            snapshot = await doc_ref.get(transaction=txn)
            if not snapshot.exists:
                return False
            data = snapshot.to_dict()
            if data.get("user_id") != user_id:
                logger.debug(
                    "[AgentNote] claim_one_time_if_due_at ownership mismatch: note=%s user=%s",
                    note_id, user_id[:8],
                )
                return False
            current_due = self._ensure_utc(data.get("due"))
            expected = self._ensure_utc(expected_due)
            if current_due != expected:
                return False
            # Re-claim guard: already fired for this due-time.
            prev_fired = self._ensure_utc(data.get("last_fired"))
            if prev_fired is not None and current_due is not None and prev_fired >= current_due:
                return False
            txn.update(doc_ref, {"last_fired": last_fired})
            return True

        return await _txn(transaction)

    async def mark_fire_delivered(self, note_id: str, due_at: datetime) -> None:
        """Stamp ``last_delivered_due`` with the fire-time we just delivered.

        No precondition — multiple calls with the same ``due_at`` are
        idempotent (final state identical), and a stamp newer than the
        worker's view (e.g. next fire arriving while we mark this one)
        is fine: we only ever advance the value forward in callers.
        """
        if not note_id:
            return
        await self._col.document(note_id).update({
            "last_delivered_due": due_at,
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_utc(dt):
        """Normalise Firestore datetimes (naive UTC) to tz-aware UTC."""
        if dt is None:
            return None
        if hasattr(dt, "tzinfo") and dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    @classmethod
    def _dict_to_note(cls, note_id: str, data: dict) -> AgentNote:
        created_at = cls._ensure_utc(data.get("created_at")) or datetime.now(timezone.utc)

        recurrence: Optional[ReminderRecurrence] = None
        if rec := data.get("recurrence"):
            recurrence = ReminderRecurrence(type=rec["type"], interval=rec.get("interval", 1))

        # Migration: existing docs without 'instruction' fall back to 'text'
        instruction = data.get("instruction") or data.get("text", "")

        complexity: Optional[TaskComplexity] = None
        if raw_complexity := data.get("complexity"):
            try:
                complexity = TaskComplexity(raw_complexity)
            except ValueError:
                logger.debug("AgentNote: unknown complexity value %r — using default", raw_complexity)

        return AgentNote(
            note_id=note_id,
            user_id=data["user_id"],
            text=data["text"],
            instruction=instruction,
            created_at=created_at,
            due=cls._ensure_utc(data["due"]),
            recurrence=recurrence,
            last_fired=cls._ensure_utc(data.get("last_fired")),
            complexity=complexity,
            last_delivered_due=cls._ensure_utc(data.get("last_delivered_due")),
        )

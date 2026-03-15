"""
FirestoreAgentNoteAdapter — persists orchestrator notes.

Collection: {env_prefix}orchestrator_notes
Document ID: epoch milliseconds string (time-sortable, collision window = 1ms)

Constraints enforced here (not at port boundary):
  - MAX_WORDS_PER_NOTE = 25
  - MAX_NOTES_PER_USER = 30
"""

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from ..config.environment import EnvironmentConfig
from ..domain.agent_note import AgentNote, NoteCreate, NoteUpdate
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
        await self._col.document(note_id).set({
            "user_id": data.user_id,
            "text": data.text,
            "created_at": now,
            "visible_after": data.visible_after,
            "expires_after": data.expires_after,
        })
        return AgentNote(
            note_id=note_id,
            user_id=data.user_id,
            text=data.text,
            created_at=now,
            visible_after=data.visible_after,
            expires_after=data.expires_after,
        )

    async def delete_note(self, note_id: str, user_id: str) -> bool:
        logger.debug("🗑️ [AgentNote] delete_note called: note_id=%r user_id=%s", note_id, user_id[:8])
        doc_ref = self._col.document(note_id)
        doc = await doc_ref.get()
        logger.debug("🗑️ [AgentNote] delete_note doc.exists=%s", doc.exists)
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
        doc_ref = self._col.document(data.note_id)
        doc = await doc_ref.get()
        if not doc.exists:
            raise ValueError(f"Note not found: {data.note_id}")
        existing = doc.to_dict()
        if existing.get("user_id") != data.user_id:
            raise ValueError(
                f"Note {data.note_id} does not belong to user {data.user_id[:8]}"
            )

        updates = {}
        if data.text is not None:
            word_count = len(data.text.split())
            if word_count > self.MAX_WORDS_PER_NOTE:
                raise ValueError(
                    f"Note text exceeds {self.MAX_WORDS_PER_NOTE} words ({word_count})"
                )
            updates["text"] = data.text
        if data.visible_after is not None:
            updates["visible_after"] = data.visible_after
        if data.expires_after is not None:
            updates["expires_after"] = data.expires_after

        if updates:
            await doc_ref.update(updates)

        updated = {**existing, **updates}
        return self._dict_to_note(data.note_id, updated)

    async def list_active_notes(self, user_id: str, as_of: datetime) -> List[AgentNote]:
        docs = await self._col.where("user_id", "==", user_id).get()
        result = []
        for doc in docs:
            note = self._dict_to_note(doc.id, doc.to_dict())
            if note.visible_after and note.visible_after > as_of:
                continue
            if note.expires_after and note.expires_after <= as_of:
                continue
            result.append(note)
        return sorted(result, key=lambda n: n.created_at)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _dict_to_note(note_id: str, data: dict) -> AgentNote:
        created_at = data.get("created_at")
        if created_at and not hasattr(created_at, "tzinfo"):
            created_at = created_at.replace(tzinfo=timezone.utc)
        elif created_at and created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        visible_after = data.get("visible_after")
        if visible_after and hasattr(visible_after, "tzinfo") and visible_after.tzinfo is None:
            visible_after = visible_after.replace(tzinfo=timezone.utc)

        expires_after = data.get("expires_after")
        if expires_after and hasattr(expires_after, "tzinfo") and expires_after.tzinfo is None:
            expires_after = expires_after.replace(tzinfo=timezone.utc)

        return AgentNote(
            note_id=note_id,
            user_id=data["user_id"],
            text=data["text"],
            created_at=created_at or datetime.now(timezone.utc),
            visible_after=visible_after,
            expires_after=expires_after,
        )

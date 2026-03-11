"""
Notes Agent
===========

Specialist agent for orchestrator notepad CRUD.
No LLM — pure Firestore I/O via AgentNotePort.

Intents: create_note, delete_note, update_note.
Notes are read by RouterAgent (list_active_notes) and injected into
the prompt context automatically — no read intent needed.
"""

from datetime import datetime, timezone
from typing import Optional

from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse
from ..domain.agent_note import NoteCreate, NoteUpdate
from ..infrastructure.agent_manifest import Intent
from ..ports.agent_note_port import AgentNotePort
from ..utils.logger import logger


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse ISO 8601 string to timezone-aware datetime. Returns None if absent."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        logger.warning("⚠️ [NotesAgent] Could not parse datetime: %r", value)
        return None


class NotesAgent(BaseAgent):
    """
    Orchestrator notepad agent — no LLM.
    Dispatches on intent string and delegates to AgentNotePort.
    """

    def __init__(self, config: AgentConfig, notes_port: AgentNotePort) -> None:
        super().__init__(config)
        self._notes = notes_port

    async def can_handle(self, message: AgentMessage) -> bool:
        if message.intent != AgentIntent.QUERY:
            return False
        intent = message.payload.get("intent")
        return intent in (Intent.CREATE_NOTE, Intent.DELETE_NOTE, Intent.UPDATE_NOTE)

    async def execute(self, message: AgentMessage) -> AgentResponse:
        intent = message.payload.get("intent")
        user_id = message.context.get("user_id")
        self._on_agent_start(intent)

        try:
            if intent == Intent.CREATE_NOTE:
                # text arrives in payload["query"] (LLM puts text in the query field);
                # visible_after / expires_after arrive via context → extra_payload → payload
                text = message.payload.get("text") or message.payload.get("query", "")
                note = await self._notes.create_note(NoteCreate(
                    user_id=user_id,
                    text=text,
                    visible_after=_parse_dt(message.payload.get("visible_after")),
                    expires_after=_parse_dt(message.payload.get("expires_after")),
                ))
                result = {"note_id": note.note_id, "status": "created"}

            elif intent == Intent.DELETE_NOTE:
                note_id = message.payload.get("note_id") or message.payload.get("query", "")
                deleted = await self._notes.delete_note(
                    note_id=note_id,
                    user_id=user_id,
                )
                if not deleted:
                    return AgentResponse.failure(
                        task_id=message.task_id,
                        agent_id=self.agent_id,
                        error=f"Note {note_id!r} not found or does not belong to this user.",
                    )
                result = {"note_id": note_id, "deleted": True}

            elif intent == Intent.UPDATE_NOTE:
                note_id = message.payload.get("note_id") or message.payload.get("query", "")
                note = await self._notes.update_note(NoteUpdate(
                    note_id=note_id,
                    user_id=user_id,
                    text=message.payload.get("text"),
                    visible_after=_parse_dt(message.payload.get("visible_after")),
                    expires_after=_parse_dt(message.payload.get("expires_after")),
                ))
                result = {"note_id": note.note_id, "status": "updated"}

            else:
                error_msg = f"Unknown intent: {intent}"
                logger.error("❌ [NotesAgent] %s", error_msg)
                return AgentResponse.failure(
                    task_id=message.task_id,
                    agent_id=self.agent_id,
                    error=error_msg,
                )

        except ValueError as exc:
            logger.error("❌ [NotesAgent] Validation error: %s", exc)
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=str(exc),
            )

        result_str = str(result)
        self._on_agent_success(len(result_str), 0, result_str)
        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result=result,
            confidence=1.0,
        )

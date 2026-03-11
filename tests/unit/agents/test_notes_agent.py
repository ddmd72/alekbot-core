"""
Unit tests for NotesAgent.

Tests cover:
- can_handle: accepts create/delete/update intents, rejects others
- create_note: delegates to port, returns note_id + status
- delete_note: delegates to port, returns deleted flag
- update_note: delegates to port, returns note_id + status
- unknown intent: returns failure response
- ValueError from port: returns failure response
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from src.agents.notes_agent import NotesAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.agent_note import AgentNote, NoteCreate, NoteUpdate
from src.infrastructure.agent_manifest import Intent
from src.ports.agent_note_port import AgentNotePort


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_ID = "user-abc123"


def _make_agent(mock_port: AgentNotePort) -> NotesAgent:
    return NotesAgent(
        config=AgentConfig(
            agent_id=f"notes_agent_{_USER_ID}",
            agent_type="notes",
            timeout_ms=5000,
            capabilities=["note_management"],
        ),
        notes_port=mock_port,
    )


def _make_message(intent: str, payload: dict) -> AgentMessage:
    return AgentMessage.create(
        sender="quick_response_agent",
        recipient=f"notes_agent_{_USER_ID}",
        intent=AgentIntent.QUERY,
        payload={"intent": intent, **payload},
        context={"user_id": _USER_ID},
    )


def _make_note(note_id: str = "1741525822123", text: str = "Remind about dentist") -> AgentNote:
    return AgentNote(
        note_id=note_id,
        user_id=_USER_ID,
        text=text,
        created_at=datetime(2026, 3, 9, 14, 30, 22, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNotesAgentCanHandle:

    @pytest.fixture
    def agent(self):
        return _make_agent(AsyncMock(spec=AgentNotePort))

    async def test_accepts_create_note(self, agent):
        msg = _make_message(Intent.CREATE_NOTE, {"text": "test note"})
        assert await agent.can_handle(msg) is True

    async def test_accepts_delete_note(self, agent):
        msg = _make_message(Intent.DELETE_NOTE, {"note_id": "123"})
        assert await agent.can_handle(msg) is True

    async def test_accepts_update_note(self, agent):
        msg = _make_message(Intent.UPDATE_NOTE, {"note_id": "123", "text": "updated"})
        assert await agent.can_handle(msg) is True

    async def test_rejects_unknown_intent(self, agent):
        msg = _make_message("search_memory", {"query": "test"})
        assert await agent.can_handle(msg) is False

    async def test_rejects_non_query_intent(self, agent):
        msg = AgentMessage.create(
            sender="router",
            recipient="notes_agent",
            intent=AgentIntent.INFORM,
            payload={"intent": Intent.CREATE_NOTE, "text": "test"},
            context={"user_id": _USER_ID},
        )
        assert await agent.can_handle(msg) is False


class TestNotesAgentCreateNote:

    @pytest.fixture
    def mock_port(self):
        return AsyncMock(spec=AgentNotePort)

    async def test_create_note_success(self, mock_port):
        note = _make_note()
        mock_port.create_note.return_value = note
        agent = _make_agent(mock_port)

        msg = _make_message(Intent.CREATE_NOTE, {
            "text": "Remind about dentist",
            "visible_after": None,
            "expires_after": None,
        })
        response = await agent.execute(msg)

        assert response.status == AgentStatus.SUCCESS
        assert response.result["note_id"] == "1741525822123"
        assert response.result["status"] == "created"

    async def test_create_note_calls_port_with_correct_data(self, mock_port):
        mock_port.create_note.return_value = _make_note()
        agent = _make_agent(mock_port)

        msg = _make_message(Intent.CREATE_NOTE, {"text": "Remind about dentist"})
        await agent.execute(msg)

        mock_port.create_note.assert_called_once()
        call_arg: NoteCreate = mock_port.create_note.call_args[0][0]
        assert call_arg.user_id == _USER_ID
        assert call_arg.text == "Remind about dentist"

    async def test_create_note_word_cap_raises_failure(self, mock_port):
        mock_port.create_note.side_effect = ValueError("Note text exceeds 25 words")
        agent = _make_agent(mock_port)

        msg = _make_message(Intent.CREATE_NOTE, {"text": "word " * 26})
        response = await agent.execute(msg)

        assert response.status == AgentStatus.FAILED
        assert "25 words" in response.error

    async def test_create_note_parses_visible_after(self, mock_port):
        mock_port.create_note.return_value = _make_note()
        agent = _make_agent(mock_port)

        msg = _make_message(Intent.CREATE_NOTE, {
            "text": "Check tomorrow",
            "visible_after": "2026-03-10T09:00:00+00:00",
        })
        await agent.execute(msg)

        call_arg: NoteCreate = mock_port.create_note.call_args[0][0]
        assert call_arg.visible_after is not None
        assert call_arg.visible_after.year == 2026


class TestNotesAgentDeleteNote:

    @pytest.fixture
    def mock_port(self):
        return AsyncMock(spec=AgentNotePort)

    async def test_delete_note_success(self, mock_port):
        mock_port.delete_note.return_value = True
        agent = _make_agent(mock_port)

        msg = _make_message(Intent.DELETE_NOTE, {"note_id": "1741525822123"})
        response = await agent.execute(msg)

        assert response.status == AgentStatus.SUCCESS
        assert response.result["deleted"] is True
        assert response.result["note_id"] == "1741525822123"

    async def test_delete_note_not_found(self, mock_port):
        mock_port.delete_note.return_value = False
        agent = _make_agent(mock_port)

        msg = _make_message(Intent.DELETE_NOTE, {"note_id": "nonexistent"})
        response = await agent.execute(msg)

        assert response.status == AgentStatus.FAILED
        assert "not found" in response.error.lower()


class TestNotesAgentUpdateNote:

    @pytest.fixture
    def mock_port(self):
        return AsyncMock(spec=AgentNotePort)

    async def test_update_note_success(self, mock_port):
        note = _make_note(text="Updated text")
        mock_port.update_note.return_value = note
        agent = _make_agent(mock_port)

        msg = _make_message(Intent.UPDATE_NOTE, {
            "note_id": "1741525822123",
            "text": "Updated text",
        })
        response = await agent.execute(msg)

        assert response.status == AgentStatus.SUCCESS
        assert response.result["note_id"] == "1741525822123"
        assert response.result["status"] == "updated"

    async def test_update_note_calls_port_with_correct_data(self, mock_port):
        mock_port.update_note.return_value = _make_note()
        agent = _make_agent(mock_port)

        msg = _make_message(Intent.UPDATE_NOTE, {
            "note_id": "1741525822123",
            "text": "New text",
        })
        await agent.execute(msg)

        call_arg: NoteUpdate = mock_port.update_note.call_args[0][0]
        assert call_arg.note_id == "1741525822123"
        assert call_arg.user_id == _USER_ID
        assert call_arg.text == "New text"


class TestNotesAgentEdgeCases:

    async def test_unknown_intent_returns_failure(self):
        mock_port = AsyncMock(spec=AgentNotePort)
        agent = _make_agent(mock_port)

        msg = _make_message("unknown_intent", {})
        response = await agent.execute(msg)

        assert response.status == AgentStatus.FAILED
        assert "unknown_intent" in response.error.lower() or "Unknown" in response.error

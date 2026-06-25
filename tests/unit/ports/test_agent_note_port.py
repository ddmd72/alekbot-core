"""
Port contract tests for AgentNotePort.

Covers:
- AgentNotePort (9 abstract async methods: create_note, delete_note,
  update_note, get_note, list_active_notes, list_due_reminders,
  reschedule_if_due_at, delete_if_due_at, mark_fire_delivered)
- AsyncMock(spec=AgentNotePort) satisfies the port contract in agent tests
"""

import inspect
import pytest
from abc import ABC
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from src.domain.agent_note import AgentNote, NoteCreate, NoteUpdate
from src.ports.agent_note_port import AgentNotePort


class TestAgentNotePortContract:
    """Verify AgentNotePort declares all required abstract methods."""

    def test_is_abstract_class(self):
        assert issubclass(AgentNotePort, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            AgentNotePort()

    def test_has_create_note(self):
        assert getattr(AgentNotePort.create_note, "__isabstractmethod__", False)

    def test_has_delete_note(self):
        assert getattr(AgentNotePort.delete_note, "__isabstractmethod__", False)

    def test_has_update_note(self):
        assert getattr(AgentNotePort.update_note, "__isabstractmethod__", False)

    def test_has_list_active_notes(self):
        assert getattr(AgentNotePort.list_active_notes, "__isabstractmethod__", False)

    def test_has_get_note(self):
        assert getattr(AgentNotePort.get_note, "__isabstractmethod__", False)

    def test_has_reschedule_if_due_at(self):
        assert getattr(AgentNotePort.reschedule_if_due_at, "__isabstractmethod__", False)

    def test_has_delete_if_due_at(self):
        assert getattr(AgentNotePort.delete_if_due_at, "__isabstractmethod__", False)

    def test_has_mark_fire_delivered(self):
        assert getattr(AgentNotePort.mark_fire_delivered, "__isabstractmethod__", False)

    def test_has_claim_one_time_if_due_at(self):
        assert getattr(AgentNotePort.claim_one_time_if_due_at, "__isabstractmethod__", False)

    def test_claim_one_time_if_due_at_is_async(self):
        assert inspect.iscoroutinefunction(AgentNotePort.claim_one_time_if_due_at)

    def test_all_abstract_methods_are_async(self):
        for name in ("create_note", "delete_note", "update_note", "get_note",
                     "list_active_notes", "list_due_reminders",
                     "reschedule_if_due_at", "delete_if_due_at", "mark_fire_delivered"):
            method = getattr(AgentNotePort, name)
            assert inspect.iscoroutinefunction(method), f"{name} must be async"

    def test_all_abstract_methods_count(self):
        abstract_methods = {
            name for name, method in inspect.getmembers(AgentNotePort)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 10, f"Expected 10 abstract methods, got {abstract_methods}"

    def test_deprecated_reschedule_is_gone(self):
        """The unconditional reschedule() was removed in Step #7 because
        it had a duplicate-fire race. reschedule_if_due_at supersedes it.
        This test guards against accidental reintroduction."""
        assert not hasattr(AgentNotePort, "reschedule")

    def test_create_note_signature(self):
        sig = inspect.signature(AgentNotePort.create_note)
        params = list(sig.parameters.keys())
        assert "self" in params
        assert "data" in params

    def test_delete_note_signature(self):
        sig = inspect.signature(AgentNotePort.delete_note)
        params = list(sig.parameters.keys())
        assert "self" in params
        assert "note_id" in params
        assert "user_id" in params

    def test_update_note_signature(self):
        sig = inspect.signature(AgentNotePort.update_note)
        params = list(sig.parameters.keys())
        assert "self" in params
        assert "data" in params

    def test_list_active_notes_signature(self):
        sig = inspect.signature(AgentNotePort.list_active_notes)
        params = list(sig.parameters.keys())
        assert "self" in params
        assert "user_id" in params
        assert "as_of" in params

    def test_get_note_signature(self):
        sig = inspect.signature(AgentNotePort.get_note)
        params = list(sig.parameters.keys())
        assert params == ["self", "user_id", "note_id"]

    def test_reschedule_if_due_at_signature(self):
        sig = inspect.signature(AgentNotePort.reschedule_if_due_at)
        params = list(sig.parameters.keys())
        assert params == ["self", "note_id", "expected_due", "next_due", "last_fired"]

    def test_delete_if_due_at_signature(self):
        sig = inspect.signature(AgentNotePort.delete_if_due_at)
        params = list(sig.parameters.keys())
        assert params == ["self", "note_id", "user_id", "expected_due"]

    def test_mark_fire_delivered_signature(self):
        sig = inspect.signature(AgentNotePort.mark_fire_delivered)
        params = list(sig.parameters.keys())
        assert params == ["self", "note_id", "due_at"]


class TestAgentNotePortMockImplementation:
    """Verify AsyncMock(spec=AgentNotePort) satisfies the port contract in agent tests."""

    @pytest.fixture
    def mock_port(self):
        return AsyncMock(spec=AgentNotePort)

    def _make_note(self, note_id: str = "1741525822123") -> AgentNote:
        return AgentNote(
            note_id=note_id,
            user_id="user-1",
            text="Remind user about dentist",
            instruction="Remind user about dentist",
            created_at=datetime(2026, 3, 9, 14, 30, 22, tzinfo=timezone.utc),
            due=datetime(2026, 3, 10, 9, 0, 0, tzinfo=timezone.utc),
        )

    async def test_create_note_returns_agent_note(self, mock_port):
        note = self._make_note()
        mock_port.create_note.return_value = note
        data = NoteCreate(user_id="user-1", text="Remind user about dentist", instruction="Remind user about dentist", due=datetime(2026, 3, 10, 9, 0, 0, tzinfo=timezone.utc))
        result = await mock_port.create_note(data)
        assert result.note_id == "1741525822123"
        assert result.text == "Remind user about dentist"

    async def test_delete_note_returns_bool(self, mock_port):
        mock_port.delete_note.return_value = True
        result = await mock_port.delete_note(note_id="1741525822123", user_id="user-1")
        assert result is True

    async def test_delete_note_not_found_returns_false(self, mock_port):
        mock_port.delete_note.return_value = False
        result = await mock_port.delete_note(note_id="nonexistent", user_id="user-1")
        assert result is False

    async def test_update_note_returns_agent_note(self, mock_port):
        note = self._make_note()
        mock_port.update_note.return_value = note
        data = NoteUpdate(note_id="1741525822123", user_id="user-1", text="Updated text")
        result = await mock_port.update_note(data)
        assert result.note_id == "1741525822123"

    async def test_list_active_notes_returns_list(self, mock_port):
        note = self._make_note()
        mock_port.list_active_notes.return_value = [note]
        result = await mock_port.list_active_notes(
            user_id="user-1", as_of=datetime.now(timezone.utc)
        )
        assert len(result) == 1
        assert result[0].text == "Remind user about dentist"

    async def test_list_active_notes_empty(self, mock_port):
        mock_port.list_active_notes.return_value = []
        result = await mock_port.list_active_notes(
            user_id="user-1", as_of=datetime.now(timezone.utc)
        )
        assert result == []

    async def test_get_note_returns_optional(self, mock_port):
        note = self._make_note()
        mock_port.get_note.return_value = note
        result = await mock_port.get_note(user_id="user-1", note_id="1741525822123")
        assert result is note

        mock_port.get_note.return_value = None
        result = await mock_port.get_note(user_id="user-1", note_id="missing")
        assert result is None

    async def test_reschedule_if_due_at_returns_bool(self, mock_port):
        mock_port.reschedule_if_due_at.return_value = True
        result = await mock_port.reschedule_if_due_at(
            note_id="1",
            expected_due=datetime(2026, 5, 1, 6, 0, tzinfo=timezone.utc),
            next_due=datetime(2026, 5, 2, 6, 0, tzinfo=timezone.utc),
            last_fired=datetime(2026, 5, 1, 6, 0, 1, tzinfo=timezone.utc),
        )
        assert result is True

        mock_port.reschedule_if_due_at.return_value = False
        result = await mock_port.reschedule_if_due_at(
            note_id="1",
            expected_due=datetime(2026, 5, 1, 6, 0, tzinfo=timezone.utc),
            next_due=datetime(2026, 5, 2, 6, 0, tzinfo=timezone.utc),
            last_fired=datetime(2026, 5, 1, 6, 0, 1, tzinfo=timezone.utc),
        )
        assert result is False

    async def test_delete_if_due_at_returns_bool(self, mock_port):
        mock_port.delete_if_due_at.return_value = True
        result = await mock_port.delete_if_due_at(
            note_id="1",
            user_id="user-1",
            expected_due=datetime(2026, 5, 1, 6, 0, tzinfo=timezone.utc),
        )
        assert result is True

    async def test_mark_fire_delivered_returns_none(self, mock_port):
        mock_port.mark_fire_delivered.return_value = None
        result = await mock_port.mark_fire_delivered(
            note_id="1", due_at=datetime(2026, 5, 1, 6, 0, tzinfo=timezone.utc)
        )
        assert result is None

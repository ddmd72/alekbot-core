"""
Wire tests for FirestoreAgentNoteAdapter.

Mock at the Firestore SDK boundary (db_client), NOT at the port level.
Per ADAPTER_WIRE_TESTING.md mandate.

Tests cover:
- create_note: generates epoch-ms note_id; validates word count; enforces note cap
- list_active_notes: filters visible_after > as_of and expires_after <= as_of
- delete_note: not found → False; ownership mismatch → False; found → True
- update_note: not found raises ValueError; ownership mismatch raises ValueError; updates fields
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from src.adapters.firestore_agent_note_adapter import FirestoreAgentNoteAdapter
from src.domain.agent_note import AgentNote, NoteCreate, NoteUpdate
from src.config.environment import EnvironmentConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_USER_ID = "user-abc123"
_OTHER_USER_ID = "user-other"
_NOW = datetime(2026, 3, 9, 14, 30, 22, tzinfo=timezone.utc)
_FUTURE = _NOW + timedelta(hours=1)
_PAST = _NOW - timedelta(hours=1)
_NOTE_ID = str(int(_NOW.timestamp() * 1000))  # epoch ms
_DEFAULT_INSTRUCTION = "Call the dentist and schedule an appointment"


@pytest.fixture
def env_config():
    cfg = MagicMock(spec=EnvironmentConfig)
    cfg.orchestrator_notes_collection = "test_orchestrator_notes"
    return cfg


@pytest.fixture
def col_mock():
    """Mock Firestore collection."""
    return MagicMock()


@pytest.fixture
def db_mock(col_mock):
    """Mock Firestore db_client. collection() returns col_mock."""
    db = MagicMock()
    db.collection.return_value = col_mock
    return db


@pytest.fixture
def adapter(db_mock, env_config):
    return FirestoreAgentNoteAdapter(db_mock, env_config)


def _make_doc_snapshot(note_id: str, data: dict, exists: bool = True) -> MagicMock:
    doc = MagicMock()
    doc.exists = exists
    doc.id = note_id
    doc.to_dict.return_value = data
    return doc


def _make_note_data(
    user_id: str = _USER_ID,
    text: str = "Remind about dentist",
    instruction: str = _DEFAULT_INSTRUCTION,
    created_at: datetime = _NOW,
    due: datetime = None,
) -> dict:
    return {
        "user_id": user_id,
        "text": text,
        "instruction": instruction,
        "created_at": created_at,
        "due": due if due is not None else _FUTURE,
    }


# ---------------------------------------------------------------------------
# create_note tests
# ---------------------------------------------------------------------------


class TestCreateNote:

    async def test_create_note_generates_epoch_ms_id(self, adapter, col_mock):
        """note_id is a 13-digit epoch-milliseconds string."""
        doc_ref = MagicMock()
        doc_ref.set = AsyncMock()
        col_mock.document.return_value = doc_ref

        # list_active_notes (called inside create_note) returns empty list
        col_mock.where.return_value.get = AsyncMock(return_value=[])

        before_ms = int(_NOW.timestamp() * 1000)
        with patch("src.adapters.firestore_agent_note_adapter.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.now.side_effect = None
            mock_dt.side_effect = None

            data = NoteCreate(user_id=_USER_ID, text="Remind about dentist", instruction=_DEFAULT_INSTRUCTION, due=_FUTURE)
            note = await adapter.create_note(data)

        assert len(note.note_id) == 13
        assert note.note_id.isdigit()
        note_id_int = int(note.note_id)
        assert abs(note_id_int - before_ms) < 5000  # within 5 seconds

    async def test_create_note_persists_to_firestore(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.set = AsyncMock()
        col_mock.document.return_value = doc_ref
        col_mock.where.return_value.get = AsyncMock(return_value=[])

        data = NoteCreate(user_id=_USER_ID, text="Check tomorrow", instruction=_DEFAULT_INSTRUCTION, due=_FUTURE)
        note = await adapter.create_note(data)

        col_mock.document.assert_called_once_with(note.note_id)
        doc_ref.set.assert_called_once()
        saved = doc_ref.set.call_args[0][0]
        assert saved["user_id"] == _USER_ID
        assert saved["text"] == "Check tomorrow"

    async def test_create_note_word_count_exactly_25_passes(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.set = AsyncMock()
        col_mock.document.return_value = doc_ref
        col_mock.where.return_value.get = AsyncMock(return_value=[])

        text = " ".join(["word"] * 25)
        data = NoteCreate(user_id=_USER_ID, text=text, instruction=_DEFAULT_INSTRUCTION, due=_FUTURE)
        note = await adapter.create_note(data)
        assert note.text == text

    async def test_create_note_word_count_exceeds_limit_raises(self, adapter, col_mock):
        col_mock.where.return_value.get = AsyncMock(return_value=[])

        data = NoteCreate(user_id=_USER_ID, text=" ".join(["word"] * 26), instruction=_DEFAULT_INSTRUCTION, due=_FUTURE)
        with pytest.raises(ValueError, match="25 words"):
            await adapter.create_note(data)

    async def test_create_note_at_cap_raises(self, adapter, col_mock):
        """30 active notes → ValueError before writing."""
        # Use a far-future due so notes survive the due > as_of filter in list_active_notes
        far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        active_docs = [
            _make_doc_snapshot(
                f"note{i}",
                _make_note_data(text=f"Note {i}", due=far_future),
            )
            for i in range(30)
        ]
        col_mock.where.return_value.get = AsyncMock(return_value=active_docs)

        data = NoteCreate(user_id=_USER_ID, text="One more note", instruction=_DEFAULT_INSTRUCTION, due=_FUTURE)
        with pytest.raises(ValueError, match="cap"):
            await adapter.create_note(data)

    async def test_create_note_returns_agent_note(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.set = AsyncMock()
        col_mock.document.return_value = doc_ref
        col_mock.where.return_value.get = AsyncMock(return_value=[])

        data = NoteCreate(user_id=_USER_ID, text="Buy groceries", instruction=_DEFAULT_INSTRUCTION, due=_FUTURE)
        note = await adapter.create_note(data)

        assert isinstance(note, AgentNote)
        assert note.user_id == _USER_ID
        assert note.text == "Buy groceries"


# ---------------------------------------------------------------------------
# list_active_notes tests
# ---------------------------------------------------------------------------


class TestListActiveNotes:

    async def test_returns_notes_with_future_due(self, adapter, col_mock):
        """Notes with due > as_of are included."""
        docs = [
            _make_doc_snapshot("n1", _make_note_data(text="Note A", due=_FUTURE)),
            _make_doc_snapshot("n2", _make_note_data(text="Note B", due=_FUTURE)),
        ]
        col_mock.where.return_value.get = AsyncMock(return_value=docs)

        notes = await adapter.list_active_notes(_USER_ID, as_of=_NOW)
        assert len(notes) == 2

    async def test_excludes_note_with_past_due(self, adapter, col_mock):
        """Notes with due <= as_of are excluded (already fired or overdue)."""
        docs = [
            _make_doc_snapshot("n1", _make_note_data(text="Overdue", due=_PAST)),
            _make_doc_snapshot("n2", _make_note_data(text="Active", due=_FUTURE)),
        ]
        col_mock.where.return_value.get = AsyncMock(return_value=docs)

        notes = await adapter.list_active_notes(_USER_ID, as_of=_NOW)
        assert len(notes) == 1
        assert notes[0].text == "Active"

    async def test_excludes_note_with_due_equal_to_as_of(self, adapter, col_mock):
        """Notes with due == as_of are excluded (fire moment has passed)."""
        docs = [
            _make_doc_snapshot("n1", _make_note_data(text="Just fired", due=_NOW)),
        ]
        col_mock.where.return_value.get = AsyncMock(return_value=docs)

        notes = await adapter.list_active_notes(_USER_ID, as_of=_NOW)
        assert len(notes) == 0

    async def test_includes_note_with_future_due(self, adapter, col_mock):
        """Notes with due > as_of are included regardless of how far in future."""
        docs = [
            _make_doc_snapshot("n1", _make_note_data(text="Far future", due=_NOW + timedelta(days=30))),
        ]
        col_mock.where.return_value.get = AsyncMock(return_value=docs)

        notes = await adapter.list_active_notes(_USER_ID, as_of=_NOW)
        assert len(notes) == 1

    async def test_all_overdue_returns_empty(self, adapter, col_mock):
        """All notes past due → empty list."""
        docs = [
            _make_doc_snapshot("n1", _make_note_data(text="Expired A", due=_PAST)),
            _make_doc_snapshot("n2", _make_note_data(text="Expired B", due=_PAST)),
        ]
        col_mock.where.return_value.get = AsyncMock(return_value=docs)

        notes = await adapter.list_active_notes(_USER_ID, as_of=_NOW)
        assert len(notes) == 0

    async def test_sorted_by_created_at_ascending(self, adapter, col_mock):
        t1 = datetime(2026, 3, 9, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 3, 9, 12, 0, 0, tzinfo=timezone.utc)
        docs = [
            _make_doc_snapshot("n2", _make_note_data(text="Later", created_at=t2)),
            _make_doc_snapshot("n1", _make_note_data(text="Earlier", created_at=t1)),
        ]
        col_mock.where.return_value.get = AsyncMock(return_value=docs)

        notes = await adapter.list_active_notes(_USER_ID, as_of=_NOW)
        assert notes[0].text == "Earlier"
        assert notes[1].text == "Later"

    async def test_empty_when_no_notes(self, adapter, col_mock):
        col_mock.where.return_value.get = AsyncMock(return_value=[])
        notes = await adapter.list_active_notes(_USER_ID, as_of=_NOW)
        assert notes == []


# ---------------------------------------------------------------------------
# delete_note tests
# ---------------------------------------------------------------------------


class TestDeleteNote:

    async def test_delete_not_found_returns_false(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_make_doc_snapshot(_NOTE_ID, {}, exists=False))
        col_mock.document.return_value = doc_ref

        result = await adapter.delete_note(_NOTE_ID, _USER_ID)
        assert result is False

    async def test_delete_ownership_mismatch_returns_false(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_make_doc_snapshot(
            _NOTE_ID, _make_note_data(user_id=_OTHER_USER_ID)
        ))
        col_mock.document.return_value = doc_ref

        result = await adapter.delete_note(_NOTE_ID, _USER_ID)
        assert result is False
        doc_ref.delete.assert_not_called()

    async def test_delete_success_returns_true(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_make_doc_snapshot(
            _NOTE_ID, _make_note_data()
        ))
        doc_ref.delete = AsyncMock()
        col_mock.document.return_value = doc_ref

        result = await adapter.delete_note(_NOTE_ID, _USER_ID)
        assert result is True
        doc_ref.delete.assert_called_once()


# ---------------------------------------------------------------------------
# update_note tests
# ---------------------------------------------------------------------------


class TestUpdateNote:

    async def test_update_not_found_raises(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_make_doc_snapshot(_NOTE_ID, {}, exists=False))
        col_mock.document.return_value = doc_ref

        upd = NoteUpdate(note_id=_NOTE_ID, user_id=_USER_ID, text="New text")
        with pytest.raises(ValueError, match="not found"):
            await adapter.update_note(upd)

    async def test_update_ownership_mismatch_raises(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_make_doc_snapshot(
            _NOTE_ID, _make_note_data(user_id=_OTHER_USER_ID)
        ))
        col_mock.document.return_value = doc_ref

        upd = NoteUpdate(note_id=_NOTE_ID, user_id=_USER_ID, text="New text")
        with pytest.raises(ValueError, match="does not belong"):
            await adapter.update_note(upd)

    async def test_update_text_success(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_make_doc_snapshot(
            _NOTE_ID, _make_note_data()
        ))
        doc_ref.update = AsyncMock()
        col_mock.document.return_value = doc_ref

        upd = NoteUpdate(note_id=_NOTE_ID, user_id=_USER_ID, text="Updated text")
        note = await adapter.update_note(upd)

        doc_ref.update.assert_called_once_with({"text": "Updated text"})
        assert note.text == "Updated text"
        assert note.note_id == _NOTE_ID

    async def test_update_text_word_count_exceeds_raises(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_make_doc_snapshot(
            _NOTE_ID, _make_note_data()
        ))
        col_mock.document.return_value = doc_ref

        upd = NoteUpdate(note_id=_NOTE_ID, user_id=_USER_ID, text=" ".join(["w"] * 26))
        with pytest.raises(ValueError, match="25 words"):
            await adapter.update_note(upd)

    async def test_update_no_fields_skips_firestore_call(self, adapter, col_mock):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_make_doc_snapshot(
            _NOTE_ID, _make_note_data()
        ))
        doc_ref.update = AsyncMock()
        col_mock.document.return_value = doc_ref

        upd = NoteUpdate(note_id=_NOTE_ID, user_id=_USER_ID)
        note = await adapter.update_note(upd)

        doc_ref.update.assert_not_called()
        assert isinstance(note, AgentNote)

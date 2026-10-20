"""workspace_file on stored reminders (AGENT_WORKSPACE_RFC §8.3): written only when set, cleared by the clear flag,
read back; documents without it read as None (no migration). Mocked at the Firestore SDK boundary
(ADAPTER_WIRE_TESTING.md)."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.firestore_agent_note_adapter import FirestoreAgentNoteAdapter
from src.config.environment import EnvironmentConfig
from src.domain.agent_note import AgentNote, NoteCreate, NoteUpdate

USER = "user-abc123"
NOTE_ID = "1729401234567"
NOW = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)
DUE = NOW + timedelta(days=1)
FILE = "projects/morning-briefing.md"


@pytest.fixture
def col():
    return MagicMock()


@pytest.fixture
def adapter(col):
    db = MagicMock()
    db.collection.return_value = col
    cfg = MagicMock(spec=EnvironmentConfig)
    cfg.orchestrator_notes_collection = "test_orchestrator_notes"
    return FirestoreAgentNoteAdapter(db, cfg)


def _doc(col, data):
    ref = MagicMock()
    snap = MagicMock()
    snap.exists, snap.id = True, NOTE_ID
    snap.to_dict.return_value = data
    ref.get = AsyncMock(return_value=snap)
    ref.update = AsyncMock()
    ref.set = AsyncMock()
    col.document.return_value = ref
    col.where.return_value.get = AsyncMock(return_value=[])
    return ref


def _stored(**extra):
    return {"user_id": USER, "text": "Morning briefing", "instruction": "Morning briefing: follow the workspace file.",
            "created_at": NOW, "due": DUE, **extra}


class TestCreate:
    async def test_writes_and_returns_the_field(self, adapter, col):
        ref = _doc(col, {})
        note = await adapter.create_note(NoteCreate(user_id=USER, text="Morning briefing", instruction="x", due=DUE,
                                                    workspace_file=FILE))
        assert ref.set.call_args[0][0]["workspace_file"] == FILE
        assert note.workspace_file == FILE

    async def test_omits_an_unset_field(self, adapter, col):
        ref = _doc(col, {})
        await adapter.create_note(NoteCreate(user_id=USER, text="x", instruction="x", due=DUE))
        assert "workspace_file" not in ref.set.call_args[0][0]


class TestUpdate:
    async def test_sets(self, adapter, col):
        ref = _doc(col, _stored())
        note = await adapter.update_note(NoteUpdate(note_id=NOTE_ID, user_id=USER, workspace_file=FILE))
        ref.update.assert_awaited_once_with({"workspace_file": FILE})
        assert note.workspace_file == FILE

    async def test_clears(self, adapter, col):
        ref = _doc(col, _stored(workspace_file=FILE))
        note = await adapter.update_note(NoteUpdate(note_id=NOTE_ID, user_id=USER, clear_workspace_file=True))
        ref.update.assert_awaited_once_with({"workspace_file": None})
        assert note.workspace_file is None

    async def test_leaves_it_alone_by_default(self, adapter, col):
        ref = _doc(col, _stored(workspace_file=FILE))
        note = await adapter.update_note(NoteUpdate(note_id=NOTE_ID, user_id=USER, text="New label"))
        ref.update.assert_awaited_once_with({"text": "New label"})
        assert note.workspace_file == FILE


class TestRead:
    async def test_old_documents_read_as_none(self, adapter, col):
        _doc(col, _stored())
        assert (await adapter.get_note(USER, NOTE_ID)).workspace_file is None

    async def test_round_trip(self, adapter, col):
        _doc(col, _stored(workspace_file=FILE))
        assert (await adapter.get_note(USER, NOTE_ID)).workspace_file == FILE


def test_domain_defaults():
    note = AgentNote(note_id="1", user_id=USER, text="x", instruction="x", created_at=NOW, due=DUE)
    assert note.workspace_file is None
    update = NoteUpdate(note_id="1", user_id=USER)
    assert (update.workspace_file, update.clear_workspace_file) == (None, False)

"""FirestoreAgentNoteAdapter on the evaluating fake: what a created note reads back as, and the conditional delete racing
a reschedule of the same fire (the mutation check's survivors). Assertions are on what the store returns and keeps."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.adapters import firestore_agent_note_adapter as note_module
from src.adapters.firestore_agent_note_adapter import FirestoreAgentNoteAdapter
from src.config.environment import EnvironmentConfig
from src.domain.agent_note import NoteCreate
from tests.support.clock_instants import pin_datetime
from tests.support.firestore_eval import EvalDb, EvalDocRef

UTC = timezone.utc
NOTES = "notes"
CREATED = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
DUE = datetime(2026, 10, 27, 8, 0, tzinfo=UTC)
NEXT_DUE = DUE + timedelta(days=1)


@pytest.fixture
def db():
    return EvalDb()


@pytest.fixture
def notes(db):
    env = MagicMock(spec=EnvironmentConfig)
    env.orchestrator_notes_collection = NOTES
    return FirestoreAgentNoteAdapter(db, env)


async def _create(notes, monkeypatch) -> str:
    pin_datetime(monkeypatch, note_module, CREATED)
    note = await notes.create_note(NoteCreate(
        user_id="u1", text="call dad", instruction="Remind the user to call dad.", due=DUE, recurrence="FREQ=DAILY",
    ))
    return note.note_id


async def test_a_created_note_reads_back_with_its_instruction_and_creation_time(notes, monkeypatch):
    note_id = await _create(notes, monkeypatch)
    pin_datetime(monkeypatch, note_module, DUE)   # a later read: the creation time must come from the store

    stored = await notes.get_note("u1", note_id)

    assert stored is not None
    assert stored.instruction == "Remind the user to call dad."
    assert stored.created_at == CREATED


async def test_a_delete_racing_a_reschedule_of_the_same_fire_loses_and_the_note_stays(notes, db, monkeypatch):
    """Both read the note at `DUE` before either writes. The reschedule commits first; the delete's transaction then
    sees the moved due and refuses, so the rescheduled note is kept."""
    note_id = await _create(notes, monkeypatch)
    db.collection(NOTES).yield_on_get = True
    dues_read = []
    read = EvalDocRef.get

    async def recording_get(self, *args, **kwargs):
        snap = await read(self, *args, **kwargs)
        dues_read.append((snap.to_dict() or {}).get("due"))
        return snap

    monkeypatch.setattr(EvalDocRef, "get", recording_get)

    rescheduled, deleted = await asyncio.gather(
        notes.reschedule_if_due_at(note_id, expected_due=DUE, next_due=NEXT_DUE, last_fired=DUE),
        notes.delete_if_due_at(note_id, "u1", expected_due=DUE),
    )

    assert dues_read[:2] == [DUE, DUE], "both read the fire's due before either wrote (the interleaving)"
    assert (rescheduled, deleted) == (True, False)
    kept = await notes.get_note("u1", note_id)
    assert kept is not None and kept.due == NEXT_DUE

"""DriveNoteLifecycle by connection (MULTI_DRIVE_RFC §8, §9, §11.6; slice 1b part D): the round end and the audit read
the tick's connection's items through its handle and its index by its scope, and act only on notes bound on that
connection (by ref tag): a note of another connection is never archived by this connection's tick."""
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Sequence

import pytest

from src.adapters.in_memory_drive_note_repo import InMemoryDriveNoteRepository
from src.domain.connection_scope import ConnectionScope
from src.domain.drive_index import DriveSyncState
from src.domain.drive_index_lean import DriveRecordLean
from src.domain.drive_note import (
    DriveNote, NoteAction, NoteBinding, NoteContent, NoteProvenance, NoteSnapshot, NoteState, next_version,
    note_after_write,
)
from src.domain.drive_note_check import WHY_GONE, NoteCheck
from src.services.drive_note_lifecycle import DriveNoteLifecycle
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connections import seed
from tests.support.fake_drive_adapter import FakeDriveAdapter

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
NOW = T0 + timedelta(days=30)


class _LeanIndex:
    """The lean record reads of the index, by connection scope; no record stored (every item left the index)."""

    def __init__(self) -> None:
        self.asked: List[ConnectionScope] = []

    async def get_records_lean(self, scope: ConnectionScope, item_ids: Sequence[str]) -> Dict[str, DriveRecordLean]:
        self.asked.append(scope)
        return {}


async def _note(notes: InMemoryDriveNoteRepository, note_id: str, item_id: str, tag: str) -> None:
    base = DriveNote(note_id=note_id, user_id="usr-1", account_id="acc-1",
                     binding=NoteBinding(identity=f"fake:x:{item_id}", item_id=item_id, ref_tag=tag),
                     snapshot=NoteSnapshot(name=f"{item_id}.pdf", snapshot_at=T0))
    version = next_version(0, NoteContent("noted"), NoteAction.SET, NoteProvenance(), T0)
    written = note_after_write(base, version, NoteState.ACTIVE, NoteProvenance(), T0, vector=None,
                               embedding_space="", embedding_pending=True, binding=base.binding)
    await notes.write_version(written, version, expected_version=0)


@pytest.fixture
async def world():
    class World:
        pass
    w = World()
    w.notes = InMemoryDriveNoteRepository()
    w.index = _LeanIndex()
    w.drive = FakeDriveAdapter("fake_a")
    repo = FakeDriveConnectionRepository()
    w.one = await seed(repo, connection_id="conn-1", tag="a1")
    here = w.drive.plant(w.one.handle, "root", "here.pdf")           # still on conn-1's drive (generated: item-1)
    await _note(w.notes, "n-gone", "item-gone", "a1")                # conn-1's note, item gone from conn-1
    await _note(w.notes, "n-other", "item-gone", "b2")               # another connection's note of the same item id
    await _note(w.notes, "n-here", here, "a1")
    w.lifecycle = DriveNoteLifecycle(w.notes, w.index, w.drive, clock=lambda: NOW)
    return w


class TestRoundEnd:
    async def test_only_the_connections_note_is_archived(self, world):
        state = DriveSyncState(user_id="usr-1", root_item_id="root", note_checks=(
            NoteCheck("n-gone", "item-gone", WHY_GONE, T0), NoteCheck("n-other", "item-gone", WHY_GONE, T0)))
        state, done, counts = await world.lifecycle.round_end(world.one, state, take_item_call=lambda: True,
                                                              fence=lambda: None)
        assert done == frozenset({"n-gone", "n-other"}) and state.note_checks == ()
        assert (await world.notes.get("usr-1", "n-gone")).state == NoteState.ARCHIVED.value
        assert (await world.notes.get("usr-1", "n-other")).state == NoteState.ACTIVE.value
        assert counts.get("note_checks_dropped") == 1 and counts.get("notes_archived") == 1
        assert set(world.index.asked) == {world.one.scope}


class TestAudit:
    async def test_the_audit_reads_the_connections_notes_through_its_handle(self, world):
        state = DriveSyncState(user_id="usr-1", root_item_id="root")
        state, counts = await world.lifecycle.audit(world.one, state, take_item_call=lambda: True,
                                                    fence=lambda: None)
        assert state.notes_audited_at == NOW
        assert counts.get("notes_audited") == 2 and counts.get("notes_archived_by_audit") == 1
        assert (await world.notes.get("usr-1", "n-gone")).state == NoteState.ARCHIVED.value
        assert (await world.notes.get("usr-1", "n-here")).state == NoteState.ACTIVE.value
        assert (await world.notes.get("usr-1", "n-other")).state == NoteState.ACTIVE.value

    async def test_no_audit_before_the_index_has_a_root(self, world):
        state = DriveSyncState(user_id="usr-1")
        assert await world.lifecycle.audit(world.one, state, take_item_call=lambda: True,
                                           fence=lambda: None) == (state, {})

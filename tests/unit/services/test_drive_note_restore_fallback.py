"""An archived note whose file comes back is re-activated even when entry (a) missed the restore (final review L-I1;
DRIVE_FILE_NOTES_RFC §7.2): the notes step's re-read finds the archived note bound to this drive with a live record and
re-activates it. One test per trigger: a note-store error during the restore, the kill switch off during the restore,
a code rollback. Real tick, write path and search over the in-memory stores. Synthetic items and notes only."""
from __future__ import annotations

from dataclasses import replace

import pytest

from src.domain.drive_extraction import PARSER, READER
from src.domain.drive_note import BoundBy, NoteState
from src.domain.drive_note_merge import NOTE_MERGE_VERSION
from src.services.drive_indexing_service import DriveIndexingService

from .drive_content_fakes import FakeExtractor, FakeTriage
from .test_drive_note_invariants import SPACE, U, World, _archived


@pytest.fixture
def w() -> World:
    return World()


def _without_notes(w: World) -> DriveIndexingService:
    """The same indexer with no note store and no lifecycle: the kill switch off, or a revision without notes."""
    return DriveIndexingService(
        w.drive, w.repo, w.emb, w.queue, w.users, lambda lat, lon: None, SPACE,
        extractors={PARSER: FakeExtractor(PARSER), READER: FakeExtractor(READER)}, triage=FakeTriage(),
        clock=w.clock, monotonic=w.clock.monotonic)


async def _back(w: World, item: str, nid: str) -> None:
    note = await w.stored(nid)
    assert note.state == NoteState.ACTIVE.value and note.binding.bound_by == BoundBy.RESTORE.value
    assert w.carriers(nid) == [item]
    await w.control(nid)


async def test_a_note_store_error_during_the_restore(w):
    item, nid = await _archived(w)
    real = w.notes.find_by_match_keys
    failed = []

    async def once(user_id, keys):
        if not failed:
            failed.append(keys)
            raise RuntimeError("note store down")
        return await real(user_id, keys)

    w.notes.find_by_match_keys = once
    w.drive.add("scan.pdf", item_id=item)
    await w.settle()
    assert failed and w.repo.records.get((U, item)) is not None
    w.clock.advance(600)
    await w.settle()
    await _back(w, item, nid)


async def test_the_kill_switch_off_during_the_restore(w):
    item, nid = await _archived(w)
    off = _without_notes(w)
    w.drive.add("scan.pdf", item_id=item)
    for _ in range(3):
        await off.tick(U, None)
        w.clock.advance(600)
    assert w.repo.records.get((U, item)) is not None and w.carriers(nid) == []
    assert (await w.repo.get_sync_state(U)).notes_merge_version == 0   # the switch's zero: a re-read follows
    await w.settle()
    await _back(w, item, nid)


async def test_a_code_rollback_during_the_restore(w):
    item, nid = await _archived(w)
    old = _without_notes(w)
    w.drive.add("scan.pdf", item_id=item)
    for _ in range(3):
        await old.tick(U, None)
        # a revision without notes support never writes the notes fields: its merge version survives (plan B12)
        state = await w.repo.get_sync_state(U)
        w.repo.states[U] = replace(state, notes_merge_version=NOTE_MERGE_VERSION)
        w.clock.advance(600)
    state = await w.repo.get_sync_state(U)
    assert w.repo.records.get((U, item)) is not None and state.last_success_at > state.notes_tick_at   # what the detector reads
    await w.settle()
    await _back(w, item, nid)

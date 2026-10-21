"""The daily notes audit (DRIVE_FILE_NOTES_RFC §7.4) and the lifecycle's own guards: a note bound to an item with no
record is read by id once a day; a not-found archives it, a found item refreshes its snapshot; the item-call budget
bounds it; a note seen in the last seven days is skipped; dormant notes are never read; the lease fence stops writes;
a note-store failure keeps the check and never ends the tick."""
from dataclasses import replace
from datetime import timedelta

import pytest

from src.domain.drive_index import DriveChange, DriveIndexStoreUnavailable, DriveSyncState, LeaseLost
from src.domain.drive_note import NoteState
from src.domain.drive_note_check import WHY_GONE, NoteCheck
from src.domain.drive_note_lifecycle import NOTE_AUDIT_EVERY, NOTE_AUDIT_SKIP_DAYS

from .drive_index_fakes import ROOT
from .drive_note_world import DRIVE, NotesWorld


def _budget(n):
    left = [n]

    def take():
        if left[0] <= 0:
            return False
        left[0] -= 1
        return True

    return take


def _no_fence():
    return None


def _state(**kw):
    return DriveSyncState(user_id="u", drive_id=DRIVE, root_item_id=ROOT, **kw)


@pytest.fixture
def w():
    return NotesWorld()


def _unseen(w, nid, days=NOTE_AUDIT_SKIP_DAYS + 1):
    note = w.note(nid)
    w.notes.notes[("u", nid)] = note.model_copy(
        update={"snapshot": note.snapshot.model_copy(update={"snapshot_at": w.clock.now - timedelta(days=days)})})


class TestAudit:

    async def test_a_record_less_note_whose_item_is_gone_is_archived(self, w):
        nid = w.note_on("gone-item", merge=False)
        _unseen(w, nid)
        state, counts = await w.lifecycle.audit("u", _state(), take_item_call=_budget(5), fence=_no_fence)
        note = w.note(nid)
        assert note.state == NoteState.ARCHIVED.value and state.notes_audited_at == w.clock.now
        assert note.last_seen_at == w.clock.now - timedelta(days=NOTE_AUDIT_SKIP_DAYS + 1)
        assert counts == {"notes_audited": 1, "notes_archived_by_audit": 1}

    async def test_a_found_item_refreshes_the_snapshot(self, w):
        folder = w.drive.add("Opaque", folder=True)
        item = w.drive.add("x.pdf", parent=folder)     # never indexed: no record for it or its folder
        nid = w.note_on(item, merge=False)
        _unseen(w, nid)
        await w.lifecycle.audit("u", _state(), take_item_call=_budget(5), fence=_no_fence)
        note = w.note(nid)
        assert note.state == NoteState.ACTIVE.value and note.snapshot.snapshot_at == w.clock.now

    async def test_an_indexed_note_is_not_read(self, w):
        a = w.drive.add("a.pdf")
        await w.settle()
        nid = w.note_on(a)
        _unseen(w, nid)
        reads = []
        w.drive.get_change = lambda u, i: reads.append(i)
        state, counts = await w.lifecycle.audit("u", await w.state(), take_item_call=_budget(5), fence=_no_fence)
        assert reads == [] and counts == {} and state.notes_audited_at == w.clock.now

    async def test_a_note_seen_in_the_last_seven_days_is_skipped(self, w):
        nid = w.note_on("gone-item", merge=False)
        _unseen(w, nid, days=NOTE_AUDIT_SKIP_DAYS - 1)
        await w.lifecycle.audit("u", _state(), take_item_call=_budget(5), fence=_no_fence)
        assert w.note(nid).state == NoteState.ACTIVE.value

    async def test_a_dormant_note_is_never_read(self, w):
        nid = w.note_on("elsewhere", merge=False, drive="d2")
        _unseen(w, nid)
        await w.lifecycle.audit("u", _state(), take_item_call=_budget(5), fence=_no_fence)
        assert w.note(nid).state == NoteState.ACTIVE.value and w.note(nid).archived_at is None

    async def test_the_item_call_budget_bounds_it_and_an_unfinished_pass_is_not_stamped(self, w):
        ids = [w.note_on(f"gone{i}", merge=False) for i in range(3)]
        for nid in ids:
            _unseen(w, nid)
        state, _ = await w.lifecycle.audit("u", _state(), take_item_call=_budget(2), fence=_no_fence)
        assert sum(w.note(n).state == NoteState.ARCHIVED.value for n in ids) == 2
        assert state.notes_audited_at is None                  # continues on the next tick
        state, _ = await w.lifecycle.audit("u", state, take_item_call=_budget(2), fence=_no_fence)
        assert all(w.note(n).state == NoteState.ARCHIVED.value for n in ids) and state.notes_audited_at is not None

    async def test_once_a_day(self, w):
        nid = w.note_on("gone-item", merge=False)
        _unseen(w, nid)
        audited = w.clock.now - NOTE_AUDIT_EVERY + timedelta(minutes=1)
        state, counts = await w.lifecycle.audit("u", _state(notes_audited_at=audited), take_item_call=_budget(5),
                                                fence=_no_fence)
        assert counts == {} and state.notes_audited_at == audited and w.note(nid).state == NoteState.ACTIVE.value

    async def test_an_error_leaves_the_note_for_the_next_audit(self, w):
        nid = w.note_on("x", merge=False)
        _unseen(w, nid)

        async def failing(user_id, item_id):
            raise TimeoutError("slow")

        w.drive.get_change = failing
        state, _ = await w.lifecycle.audit("u", _state(), take_item_call=_budget(5), fence=_no_fence)
        assert w.note(nid).state == NoteState.ACTIVE.value and state.notes_audited_at == w.clock.now

    async def test_without_a_drive_id_nothing_runs(self, w):
        state, counts = await w.lifecycle.audit("u", DriveSyncState(user_id="u"), take_item_call=_budget(5),
                                                fence=_no_fence)
        assert counts == {} and state.notes_audited_at is None

    async def test_the_audit_runs_in_the_tick_and_is_stamped(self, w):
        a = w.drive.add("a.pdf")
        await w.settle()
        nid = w.note_on("vanished", merge=False)
        _unseen(w, nid)
        w.clock.advance(NOTE_AUDIT_EVERY.total_seconds())
        await w.tick()
        assert w.note(nid).state == NoteState.ARCHIVED.value
        assert (await w.state()).notes_audited_at == w.clock.now and w.rec(a) is not None


class TestGuards:

    async def test_the_lease_fence_stops_the_archive_write(self, w):
        nid = w.note_on("gone-item", merge=False)
        state = _state(note_checks=(NoteCheck(nid, "gone-item", WHY_GONE, None),))

        def fence():
            raise LeaseLost("ending")

        with pytest.raises(LeaseLost):
            await w.lifecycle.round_end("u", state, take_item_call=_budget(5), fence=fence)
        assert w.note(nid).state == NoteState.ACTIVE.value

    async def test_a_note_store_failure_keeps_the_check_and_does_not_raise(self, w):
        nid = w.note_on("gone-item", merge=False)
        state = _state(note_checks=(NoteCheck(nid, "gone-item", WHY_GONE, None),))

        async def broken(*args, **kwargs):
            raise DriveIndexStoreUnavailable("down")

        w.notes.archive = broken
        after, done, _ = await w.lifecycle.round_end("u", state, take_item_call=_budget(5), fence=_no_fence)
        assert done == frozenset() and after.note_checks == state.note_checks

    async def test_an_unreadable_note_store_keeps_every_check(self, w):
        state = _state(note_checks=(NoteCheck("n1", "i1", WHY_GONE, None),))

        async def broken(*args, **kwargs):
            raise DriveIndexStoreUnavailable("down")

        w.notes.get_many = broken
        after, done, counts = await w.lifecycle.round_end("u", state, take_item_call=_budget(5), fence=_no_fence)
        assert after == state and done == frozenset() and counts == {"note_checks_kept": 1}

    async def test_a_check_for_a_note_now_bound_elsewhere_or_erased_is_dropped(self, w):
        nid = w.note_on("other-item", merge=False)
        state = _state(note_checks=(NoteCheck(nid, "first-item", WHY_GONE, None), NoteCheck("missing", "i", WHY_GONE, None)))
        reads = []
        w.drive.get_change = lambda u, i: reads.append(i)
        after, done, _ = await w.lifecycle.round_end("u", state, take_item_call=_budget(5), fence=_no_fence)
        assert after.note_checks == () and done == {nid, "missing"} and reads == []

    async def test_an_archived_note_is_done_without_a_read(self, w):
        nid = w.note_on("gone-item", merge=False)
        w.notes.notes[("u", nid)] = w.note(nid).model_copy(update={"state": NoteState.ARCHIVED.value})
        reads = []
        w.drive.get_change = lambda u, i: reads.append(i)
        state = _state(note_checks=(NoteCheck(nid, "gone-item", WHY_GONE, None),))
        after, done, _ = await w.lifecycle.round_end("u", state, take_item_call=_budget(5), fence=_no_fence)
        assert done == {nid} and reads == []

    async def test_a_cleared_note_is_stamped_and_stays_cleared(self, w):
        nid = w.note_on("gone-item", merge=False)
        w.notes.notes[("u", nid)] = w.note(nid).model_copy(update={"state": NoteState.CLEARED.value, "text": ""})
        state = _state(note_checks=(NoteCheck(nid, "gone-item", WHY_GONE, None),))
        await w.lifecycle.round_end("u", state, take_item_call=_budget(5), fence=_no_fence)
        note = w.note(nid)
        assert note.state == NoteState.CLEARED.value and note.archived_at is not None

    async def test_the_archived_path_is_refreshed_from_the_parent_chain(self, w):
        top = w.drive.add("Work", folder=True)
        sub = w.drive.add("Q3", folder=True, parent=top)
        await w.settle()
        nid = w.note_on("gone-item", merge=False, path="old/place.pdf")
        note = w.note(nid)
        w.notes.notes[("u", nid)] = note.model_copy(
            update={"snapshot": note.snapshot.model_copy(update={"name": "place.pdf", "parent_item_id": sub})})
        state = replace(await w.state(), note_checks=(NoteCheck(nid, "gone-item", WHY_GONE, None),))
        await w.lifecycle.round_end("u", state, take_item_call=_budget(5), fence=_no_fence)
        assert w.note(nid).state == NoteState.ARCHIVED.value and w.note(nid).snapshot.path == "Work/Q3/place.pdf"

    async def test_without_the_parent_record_the_last_known_path_stays(self, w):
        nid = w.note_on("gone-item", merge=False, path="old/place.pdf")
        note = w.note(nid)
        w.notes.notes[("u", nid)] = note.model_copy(
            update={"snapshot": note.snapshot.model_copy(update={"parent_item_id": "no-record"})})
        state = _state(note_checks=(NoteCheck(nid, "gone-item", WHY_GONE, None),))
        await w.lifecycle.round_end("u", state, take_item_call=_budget(5), fence=_no_fence)
        assert w.note(nid).snapshot.path == "old/place.pdf"

    async def test_a_port_still_reporting_a_deletion_reads_as_not_found(self, w):
        nid = w.note_on("gone-item", merge=False)

        async def deleted(user_id, item_id):
            return DriveChange(item_id=item_id, deleted=True)

        w.drive.get_change = deleted
        state = _state(note_checks=(NoteCheck(nid, "gone-item", WHY_GONE, None),))
        await w.lifecycle.round_end("u", state, take_item_call=_budget(5), fence=_no_fence)
        assert w.note(nid).state == NoteState.ARCHIVED.value

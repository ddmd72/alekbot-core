"""Owner notes in the indexer tick (DRIVE_FILE_NOTES_RFC §7.1, §8, §10, review C1): a record that carried a note and
left the index reports a check; the round end archives only a `gone` delete that one `get_change` confirms as not
found; a record dropped because it is no longer stored never archives; a purge archives nothing; checks are folded into
the state at every save and carry over past the round end's caps."""
from dataclasses import replace

import pytest

from src.domain import drive_index_planner as planner
from src.domain import drive_index_policy as policy
from src.domain.drive_index import DriveChange, Scope
from src.domain.drive_note import NoteState
from src.domain.drive_note_check import WHY_GONE, WHY_UNSTORED, NoteCheck
from src.domain.user_drive import DriveConnection, DriveThrottled
from src.services import drive_indexing_service as svc_mod
from src.domain import drive_note_lifecycle as rules_mod
from src.services import drive_note_lifecycle as lifecycle_mod

from .drive_index_fakes import ROOT
from .drive_note_world import NotesWorld


@pytest.fixture
def w():
    return NotesWorld()


def _reads(w):
    """Count `get_change` calls by item."""
    calls = []
    orig = w.drive.get_change

    async def spy(user_id, item_id):
        calls.append(item_id)
        return await orig(user_id, item_id)

    w.drive.get_change = spy
    return calls


async def _noted(w, name="a.pdf", parent=ROOT):
    item = w.drive.add(name, parent=parent)
    await w.settle()
    return item, w.note_on(item)


async def _next_round(w):
    """Settle, then one more tick: a delete decided in the work-off (a cascade, a rescope, the sweep) comes after that
    tick's round end, so the next tick's round end (the next fan-out at the latest) works its check."""
    await w.settle()
    await w.tick()


class TestTombstone:

    async def test_archived_at_the_round_end_after_get_change_says_not_found(self, w):
        a, nid = await _noted(w)
        seen = (await w.state()).observed_as_of
        reads = _reads(w)
        w.drive.delete(a)
        await w.settle()
        note = w.note(nid)
        assert note.state == NoteState.ARCHIVED.value and note.archived_at is not None
        assert note.last_seen_at == seen and reads == [a]
        assert w.rec(a) is None and (await w.state()).note_checks == ()

    async def test_found_by_id_stays_active_and_its_snapshot_is_refreshed(self, w):
        a, nid = await _noted(w)
        w.drive.log.append(DriveChange(item_id=a, deleted=True))     # delta says deleted; the item is still there
        w.drive.items[a] = replace(w.drive.items[a], name="renamed.pdf")
        await w.settle()
        note = w.note(nid)
        assert note.state == NoteState.ACTIVE.value and note.snapshot.name == "renamed.pdf"
        assert note.snapshot.path == "renamed.pdf" and (await w.state()).note_checks == ()

    async def test_an_error_keeps_the_check_for_the_next_round_end(self, w):
        a, nid = await _noted(w)
        orig = w.drive.get_change
        failures = [TimeoutError("slow")]

        async def flaky(user_id, item_id):
            if failures:
                raise failures.pop()
            return await orig(user_id, item_id)

        w.drive.get_change = flaky
        w.drive.delete(a)
        await w.tick()
        assert w.note(nid).state == NoteState.ACTIVE.value
        assert [c.note_id for c in (await w.state()).note_checks] == [nid]
        await w.tick()
        assert w.note(nid).state == NoteState.ARCHIVED.value and (await w.state()).note_checks == ()

    async def test_delete_and_restore_in_one_round_never_archives(self, w):
        a, nid = await _noted(w)
        reads = _reads(w)
        w.drive.delete(a)
        w.drive.add("a.pdf", item_id=a)          # a recycle-bin restore, in the same page
        await w.settle()
        assert w.note(nid).state == NoteState.ACTIVE.value and w.rec(a) is not None
        assert reads == [] and (await w.state()).note_checks == ()


class TestStaysActiveWhenNoLongerStored:
    """Review C1: the record leaves the index, the file does not leave the drive."""

    async def _active_and_checked(self, w, nid, item):
        note = w.note(nid)
        assert note.state == NoteState.ACTIVE.value and note.archived_at is None
        assert w.rec(item) is None and (await w.state()).note_checks == ()
        return note

    async def test_moved_into_a_repository_folder(self, w):
        site = w.drive.add("site", folder=True)
        w.drive.add(".git", parent=site, folder=True)
        a, nid = await _noted(w)
        assert w.rec(site).scope is Scope.OPAQUE_ROOT
        w.drive.move(a, site)
        await w.settle()
        note = await self._active_and_checked(w, nid, a)
        assert note.snapshot.path == "site/a.pdf" and note.snapshot.parent_item_id == site

    async def test_its_folder_gains_git(self, w):
        folder = w.drive.add("project", folder=True)
        a, nid = await _noted(w, parent=folder)
        w.drive.add(".git", parent=folder, folder=True)
        await _next_round(w)
        note = await self._active_and_checked(w, nid, a)
        assert note.snapshot.path == "project/a.pdf"

    async def test_its_folder_becomes_dense(self, monkeypatch):
        monkeypatch.setattr(policy, "DENSE_FOLDER_CHILDREN", 5)
        monkeypatch.setattr(planner, "DENSE_FOLDER_CHILDREN", 5)
        w = NotesWorld(reemit_parents=True)
        dump = w.drive.add("dump", folder=True)
        a, nid = await _noted(w, parent=dump)
        for i in range(12):
            w.drive.add(f"p{i}.bin", parent=dump)
        await _next_round(w)
        assert w.rec(dump).scope is not Scope.NORMAL
        await self._active_and_checked(w, nid, a)

    async def test_renamed_into_an_os_artefact(self, w):
        """Today's planner keeps an OS artefact's record (no card, reason os_artifact): nothing leaves the index, no
        check is reported and the note stays on the record (the RFC's §7.1 table lists the rename as a delete; the code
        decides)."""
        a, nid = await _noted(w)
        reads = _reads(w)
        w.drive.rename(a, "~$a.pdf")
        await w.settle()
        assert w.note(nid).state == NoteState.ACTIVE.value and w.rec(a).owner_note_id == nid
        assert reads == [] and (await w.state()).note_checks == ()

    async def test_unstored_and_not_found_still_never_archives(self, w):
        site = w.drive.add("site", folder=True)
        w.drive.add(".git", parent=site, folder=True)
        a, nid = await _noted(w)
        w.drive.move(a, site)
        orig = w.drive.get_change

        async def gone(user_id, item_id):
            if item_id == a:
                from src.domain.user_drive import DriveItemNotFoundError
                raise DriveItemNotFoundError(item_id)
            return await orig(user_id, item_id)

        w.drive.get_change = gone
        await w.settle()
        assert w.note(nid).state == NoteState.ACTIVE.value and (await w.state()).note_checks == ()


class TestCascadeAndSweep:

    async def test_a_deleted_folder_archives_the_folder_note_and_each_noted_descendant(self, w):
        folder = w.drive.add("Contracts", folder=True)
        a = w.drive.add("a.pdf", parent=folder)
        b = w.drive.add("b.pdf", parent=folder)
        await w.settle()
        na, nf = w.note_on(a), w.note_on(folder)
        w.drive.delete(folder)
        await _next_round(w)
        assert w.note(na).state == NoteState.ARCHIVED.value and w.note(nf).state == NoteState.ARCHIVED.value
        assert w.rec(b) is None and w.rec(folder) is None

    async def test_the_cascade_reports_gone(self, w):
        folder = w.drive.add("Contracts", folder=True)
        a = w.drive.add("a.pdf", parent=folder)
        await w.settle()
        # a child the feed never tombstones (only the folder's record knows it): only the cascade deletes it
        w.repo.records[("u", "ghost")] = replace(w.rec(a), item_id="ghost", name="ghost.pdf")
        nid = w.note_on("ghost")
        w.drive.delete(folder)
        await _next_round(w)
        assert w.rec("ghost") is None and w.note(nid).state == NoteState.ARCHIVED.value

    async def test_the_epoch_sweep_reports_gone(self, w):
        a, nid = await _noted(w)
        w.drive.items.pop(a)              # gone, and the log never says so
        w.drive.resync_next = True
        await _next_round(w)
        assert w.rec(a) is None and w.note(nid).state == NoteState.ARCHIVED.value


class TestFoldedAtEverySave:

    async def test_the_check_is_saved_with_the_page_before_the_round_end(self, w):
        a, nid = await _noted(w)
        w.drive.delete(a)

        async def broken(*args, **kwargs):
            raise RuntimeError("round end broke")

        w.lifecycle.round_end = broken
        assert (await w.tick()).status == "error"         # the error path saves the state it holds
        assert (await w.state()).note_checks == (NoteCheck(nid, a, WHY_GONE, (await w.state()).note_checks[0].last_seen_at),)
        del w.lifecycle.round_end
        await w.tick()
        assert w.note(nid).state == NoteState.ARCHIVED.value

    async def test_a_throttled_round_end_keeps_the_check(self, w):
        a, nid = await _noted(w)
        w.drive.delete(a)
        orig = w.drive.get_change
        throttled = [DriveThrottled(retry_after_s=5)]

        async def throttling(user_id, item_id):
            if throttled:
                raise throttled.pop()
            return await orig(user_id, item_id)

        w.drive.get_change = throttling
        assert (await w.tick()).status == "throttled"
        assert [c.note_id for c in (await w.state()).note_checks] == [nid]
        w.clock.advance(10)
        await w.tick()
        assert w.note(nid).state == NoteState.ARCHIVED.value

    async def test_checks_carry_over_past_the_round_end_cap(self, w, monkeypatch):
        monkeypatch.setattr(lifecycle_mod, "NOTE_CHECKS_MAX", 2)
        items = [w.drive.add(f"f{i}.pdf") for i in range(3)]
        await w.settle()
        ids = [w.note_on(i) for i in items]
        for i in items:
            w.drive.delete(i)
        await w.tick()
        assert [w.note(n).state for n in ids] == [NoteState.ARCHIVED.value] * 2 + [NoteState.ACTIVE.value]
        assert [c.note_id for c in (await w.state()).note_checks] == [ids[2]]
        await w.tick()
        assert w.note(ids[2]).state == NoteState.ARCHIVED.value

    async def test_checks_carry_over_past_the_item_call_budget(self, w, monkeypatch):
        items = [w.drive.add(f"f{i}.pdf") for i in range(3)]
        await w.settle()
        ids = [w.note_on(i) for i in items]
        monkeypatch.setattr(svc_mod, "ITEM_CALLS_PER_TICK", 1)
        for i in items:
            w.drive.delete(i)
        await w.tick()
        assert sum(w.note(n).state == NoteState.ARCHIVED.value for n in ids) == 1
        await w.tick()
        await w.tick()
        assert all(w.note(n).state == NoteState.ARCHIVED.value for n in ids)

    async def test_past_the_stored_cap_the_newest_are_dropped(self, w, monkeypatch):
        monkeypatch.setattr(svc_mod, "fold_checks",
                            lambda stored, new, cap=None: rules_mod.fold_checks(stored, new, cap=1))
        items = [w.drive.add(f"f{i}.pdf") for i in range(2)]
        await w.settle()
        for i in items:
            w.note_on(i)

        async def broken(*args, **kwargs):
            raise RuntimeError("round end broke")

        w.lifecycle.round_end = broken
        for i in items:
            w.drive.delete(i)
        await w.tick()
        assert len((await w.state()).note_checks) == 1


class TestPurgeNeverArchives:

    async def _noted_with_stored_check(self, w):
        a, nid = await _noted(w)
        st = await w.state()
        await w.repo.save_sync_state(replace(st, note_checks=(NoteCheck(nid, a, WHY_GONE, None),)))
        return a, nid

    async def test_opt_out(self, w):
        a, nid = await self._noted_with_stored_check(w)
        reads = _reads(w)
        w.users.enabled["u"] = False
        await w.settle()
        assert w.rec(a) is None and await w.state() is None
        assert w.note(nid).state == NoteState.ACTIVE.value and reads == []

    async def test_rebuild(self, w):
        a, nid = await self._noted_with_stored_check(w)
        reads = _reads(w)
        await w.repo.save_sync_state(replace(await w.state(), purging=True))
        await w.settle()
        assert w.note(nid).state == NoteState.ACTIVE.value and reads.count(a) == 0

    async def test_access_loss_past_its_grace(self, w):
        from src.domain.drive_index_params import ACCESS_LOSS_TTL
        a, nid = await self._noted_with_stored_check(w)
        w.drive.connection_state = DriveConnection(connected=False)
        await w.tick()
        w.clock.advance(ACCESS_LOSS_TTL.total_seconds() + 60)
        await w.settle()
        assert w.rec(a) is None and w.note(nid).state == NoteState.ACTIVE.value

    async def test_a_different_drive(self, w):
        a, nid = await _noted(w)
        reads = _reads(w)
        orig = w.drive.get_changes

        async def other_drive(handle, cursor):
            page = await orig(handle, cursor)
            return replace(page, drive_identity="fake:d2")

        w.drive.get_changes = other_drive
        w.drive.connection_state = DriveConnection(connected=True, drive_id="d2")
        await w.settle()
        assert w.note(nid).state == NoteState.ACTIVE.value and a not in reads
        assert (await w.state()).drive_identity == "fake:d2" and (await w.state()).note_checks == ()


class TestWithoutTheLifecycle:

    async def test_no_check_is_collected_and_no_note_is_touched(self):
        w = NotesWorld(lifecycle=False)
        a, nid = await _noted(w)
        before = w.note(nid)
        w.drive.delete(a)
        await w.settle()
        assert (await w.state()).note_checks == () and w.note(nid) == before


async def test_the_why_follows_the_delete_site(w):
    """`_apply` reports a tombstone as gone and a no-longer-stored item as unstored (the checks the round end reads)."""
    a, na = await _noted(w)
    site = w.drive.add("site", folder=True)
    w.drive.add(".git", parent=site, folder=True)
    b, nb = await _noted(w, name="b.pdf")
    w.drive.delete(a)
    w.drive.move(b, site)

    async def broken(*args, **kwargs):
        raise RuntimeError("round end broke")

    w.lifecycle.round_end = broken
    await w.tick()
    assert {(c.note_id, c.why) for c in (await w.state()).note_checks} == {(na, WHY_GONE), (nb, WHY_UNSTORED)}


class TestSnapshotRefresh:
    """§7.5: a noted record renamed, moved or edited refreshes the note's snapshot from the record at the round end,
    with no item call."""

    async def test_a_rename_and_a_move(self, w):
        folder = w.drive.add("Contracts", folder=True)
        a, nid = await _noted(w)
        before = w.note(nid).snapshot
        reads = _reads(w)
        w.clock.advance(60)
        w.drive.rename(a, "invoice.pdf")
        w.drive.move(a, folder)
        await w.settle()
        snap = w.note(nid).snapshot
        assert (snap.name, snap.path, snap.parent_item_id) == ("invoice.pdf", "Contracts/invoice.pdf", folder)
        assert snap.snapshot_at > before.snapshot_at and reads == []
        assert w.note(nid).state == NoteState.ACTIVE.value

    async def test_an_unchanged_observation_writes_nothing(self, w):
        a, nid = await _noted(w)
        revision = w.note(nid).revision
        w.drive.reemit(a)
        await w.settle()
        assert w.note(nid).revision == revision

    async def test_a_folder_rename_leaves_a_descendants_path_last_known(self, w):
        """Named residual (§7.5): only the noted item's own observation refreshes its snapshot."""
        folder = w.drive.add("Contracts", folder=True)
        a, nid = await _noted(w, parent=folder)
        path = w.note(nid).snapshot.path
        w.drive.rename(folder, "Signed")
        await w.settle()
        assert w.note(nid).snapshot.path == path


def test_the_hook_does_nothing_outside_a_tick():
    svc_mod.note_snapshot_due("n1", "i1")      # no accumulator: no error, nothing kept


class TestOnlyRecordsThatLeft:
    """Review minor: a check is made in `_commit` for a record it really deletes; a delete site that names records
    that stay (a rescope page, an upgrade page) reports none."""

    async def test_a_rescope_that_keeps_the_children_reports_no_check(self, w):
        folder = w.drive.add("Contracts", folder=True)
        a, nid = await _noted(w, parent=folder)
        w.repo.records[("u", folder)] = replace(w.rec(folder), rescope_pending=True, due_at=w.clock.now,
                                                priority=3)
        seen = []
        real = w.lifecycle.round_end

        async def spy(user_id, state, **kwargs):
            seen.append(state.note_checks)
            return await real(user_id, state, **kwargs)

        w.lifecycle.round_end = spy
        await w.tick()
        assert w.rec(a) is not None and w.rec(folder).rescope_pending is False
        assert (await w.state()).note_checks == ()
        await w.tick()
        assert seen[-1] == ()                     # nothing ever reached a round end

    async def test_an_upgrade_page_reports_no_check(self, w):
        a, nid = await _noted(w)
        st = await w.state()
        await w.repo.save_sync_state(replace(st, applied_versions={**st.applied_versions, "policy": 0}))
        await w.settle()
        assert w.rec(a) is not None and (await w.state()).note_checks == ()
        assert w.note(nid).state == NoteState.ACTIVE.value


class TestTheTickHasItsNoteContext:

    async def test_a_deleted_noted_record_yields_a_check(self, w):
        """Re-review item 9 guard: the tick's `_TickNotes` exists whenever the lifecycle is wired (this world wires no
        note store), so a deleted noted record is collected. Fails if the context is None."""
        a, nid = await _noted(w)
        w.drive.delete(a)

        async def broken(*args, **kwargs):
            raise RuntimeError("round end broke")

        w.lifecycle.round_end = broken
        await w.tick()
        checks = (await w.state()).note_checks
        assert checks and checks[0].note_id == nid and checks[0].item_id == a


class TestRoundEndRotation:
    """D-C20: 25 checks whose reads keep failing, the default 20 item calls a tick. The round end takes 13 per tick
    (NOTE_ROUND_END_CALL_SHARE), moves every unknown check to the back, so it completes a rotation over all 25 in two
    ticks, and each tick keeps the rest of its calls."""

    async def test_failing_checks_rotate_and_never_starve_the_tick(self, w, monkeypatch):
        budgets = []
        real_budget = svc_mod._Budget

        def budget(*args, **kwargs):
            made = real_budget(*args, **kwargs)
            budgets.append(made)
            return made

        monkeypatch.setattr(svc_mod, "_Budget", budget)
        items = [w.drive.add(f"f{i:02d}.pdf") for i in range(25)]
        await w.settle()
        for i in items:
            w.note_on(i)
        reads = []

        async def failing(user_id, item_id):
            reads.append(item_id)
            raise TimeoutError("slow")

        for i in items:
            w.drive.delete(i)
        w.drive.get_change = failing
        await w.tick()
        assert len(reads) == 13 and budgets[-1].item_calls == 7
        first = list(reads)
        order = [c.item_id for c in (await w.state()).note_checks]
        assert order[-13:] == first                          # the unknown ones went to the back, in order
        await w.tick()
        assert set(reads) == set(items) and len(reads) == 26  # a full rotation (and the first one again)
        assert budgets[-1].item_calls == 7
        assert all(w.note(f"n-{i}").state == NoteState.ACTIVE.value for i in items)
        assert len((await w.state()).note_checks) == 25        # nothing lost

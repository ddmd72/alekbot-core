"""DriveTranscriptMirror: the root's loss (USER_DRIVE_INDEXING_RFC §27.9.9, §27.9.2; Δ184, Δ200, Δ203; plan Task 21B).
The root's record going away sets the root-loss mark (the tick, Task 20B); the step acts only after a complete round,
MIRROR_ROOT_GRACE and a not-found read. Synthetic items only, on the in-memory repositories and a scripted drive."""
from __future__ import annotations

from datetime import timedelta
from typing import List, Set

import pytest

from src.domain.drive_index import DriveIndexRecord, Reason, RecordState
from src.domain.drive_index_params import MIRROR_AUDIT_DAYS, MIRROR_ROOT_GRACE
from src.domain.drive_mirror import KIND_ARCHIVE, MIRROR_ACTIVE, MIRROR_ARCHIVED, ROOT_NAME
from src.domain.drive_mirror_plan import RootDecision, root_decision

from .drive_mirror_fakes import AREA, SCOPE, USER, Crash, MirrorWorld, Node

HOUR = timedelta(hours=1)
WEEK = timedelta(days=MIRROR_AUDIT_DAYS)


async def world_with_archive():
    """`Box/a.mp4`, `b.mp4` mirrored, `gone.mp4` archived; the root has its (opaque) folder record, as the tick keeps."""
    w = MirrorWorld()
    box = await w.folder("Box")
    a = await w.recording("a.mp4", box)
    b = await w.recording("b.mp4")
    gone = await w.recording("gone.mp4")
    await w.run()
    await w.delete_source(gone)
    w.advance(2 * HOUR)
    await w.run()
    assert w.doc(gone).state == MIRROR_ARCHIVED
    await root_record(w)
    return w, a, b, gone


async def root_record(w: MirrorWorld, name: str = ROOT_NAME, parent: str = AREA) -> None:
    await w.put(DriveIndexRecord(USER, w.root.root_item_id, parent_id=parent, name=name, is_folder=True,
                                 state=RecordState.METADATA_ONLY, reason=Reason.FOLDER))


def cut(w: MirrorWorld, item_id: str) -> List[Node]:
    """The subtree leaves the App Folder (the recycle bin, or moved out: the App Folder token answers 404 either way)."""
    nodes = [n for n in list(w.drive.nodes.values()) if w.drive.under(n.item_id, item_id)]
    for node in nodes:
        del w.drive.nodes[node.item_id]
    return nodes


def paste(w: MirrorWorld, nodes: List[Node]) -> None:
    for node in nodes:
        w.drive.nodes[node.item_id] = node


async def root_leaves(w: MirrorWorld) -> List[Node]:
    """What the tick does when the root's record goes (Task 20B): the mark, then the record delete."""
    root_id = w.root.root_item_id
    nodes = cut(w, root_id)
    assert await w.mirror.mark_root_gone(USER, root_id, w.clock)
    await w.repo.delete_records(SCOPE, [root_id])
    return nodes


def written_ids(w: MirrorWorld) -> Set[str]:
    out: Set[str] = set()
    for _, args in w.drive.writes():
        out |= {args.get("item_id"), args.get("parent_id"), args.get("new_parent_id")}
    return out - {None}


def root_reads(w: MirrorWorld, root_id: str):
    return [c for c in w.drive.calls if c[0] == "get_change" and c[1]["item_id"] == root_id]


# -- the pure decision ------------------------------------------------------------------------------------------------

def test_root_decision_graced_and_confirmed():
    from dataclasses import replace

    from src.domain.drive_mirror import MirrorRoot
    from .drive_mirror_fakes import DRIVE_IDENTITY, START
    root = MirrorRoot(USER, DRIVE_IDENTITY, "r", ROOT_NAME, root_gone_at=START)
    late = START + MIRROR_ROOT_GRACE
    assert root_decision(root, record_present=True, round_complete=True, now=late) is RootDecision.CLEAR
    assert root_decision(root, record_present=False, round_complete=True, now=late - HOUR) is RootDecision.WAIT
    assert root_decision(root, record_present=False, round_complete=False, now=late) is RootDecision.WAIT
    assert root_decision(root, record_present=False, round_complete=True, now=late) is RootDecision.CHECK
    assert root_decision(root, record_present=False, round_complete=True, now=late,
                         change_found=True) is RootDecision.CLEAR
    assert root_decision(root, record_present=False, round_complete=True, now=late,
                         change_found=False) is RootDecision.LOST
    assert root_decision(replace(root, root_gone_at=None), record_present=False, round_complete=True,
                         now=late) is RootDecision.CLEAR


# -- while the mark is set --------------------------------------------------------------------------------------------

async def test_step_stops_while_the_root_loss_mark_is_set():
    w, a, b, gone = await world_with_archive()
    await root_leaves(w)
    await w.recording("new.mp4")                           # due work and a ripe mark wait too
    await w.delete_source(b)
    snapshot = dict(w.mirror.docs)
    for step in (HOUR, 20 * HOUR, 2 * HOUR):               # inside the grace, then one tick before its end
        w.advance(step)
        w.reset_calls()
        await w.run()
        assert w.drive.calls == [] and w.mirror.docs == snapshot
    w.advance(2 * HOUR)                                    # past the grace, but the round is not complete
    w.reset_calls()
    await w.run(round_complete=False)
    assert w.drive.calls == [] and w.mirror.docs == snapshot
    assert w.root.root_gone_at is not None and w.root.previous_root_ids == ()


async def test_root_deleted_and_restored_within_the_grace_changes_nothing():
    w, a, b, gone = await world_with_archive()
    root_id = w.root.root_item_id
    snapshot = dict(w.mirror.docs)
    nodes = await root_leaves(w)
    w.advance(5 * HOUR)
    await w.run()
    paste(w, nodes)                                        # a recycle-bin restore: the same item id
    await root_record(w)                                   # observed again; the tick has not cleared the mark
    w.advance(HOUR)
    w.reset_calls()
    counters = await w.run()
    assert counters["mirror_root_mark_cleared"] == 1 and w.root.root_gone_at is None
    assert w.last_host.root_marks == [(root_id, False)]    # the tick's own list is told (carry)
    assert root_reads(w, root_id) == []                    # the record decides; no read needed
    assert w.drive.writes() == [] and w.mirror.docs == snapshot
    assert w.root.root_item_id == root_id and w.root.previous_root_ids == ()


async def test_root_found_clears_the_mark():
    w, a, b, gone = await world_with_archive()
    root_id = w.root.root_item_id
    snapshot = dict(w.mirror.docs)
    await w.mirror.mark_root_gone(USER, root_id, w.clock)  # the record went (a stray tombstone); the root did not
    await w.repo.delete_records(SCOPE, [root_id])
    w.advance(MIRROR_ROOT_GRACE + HOUR)
    w.reset_calls()
    counters = await w.run()
    assert len(root_reads(w, root_id)) == 1
    assert counters["mirror_root_mark_cleared"] == 1 and w.root.root_gone_at is None
    assert w.last_host.root_marks == [(root_id, False)]
    assert w.drive.writes() == [] and w.mirror.docs == snapshot and w.root.previous_root_ids == ()


async def test_root_read_error_waits_and_confirms_nothing():
    w, a, b, gone = await world_with_archive()
    root_id = w.root.root_item_id
    await root_leaves(w)
    w.advance(MIRROR_ROOT_GRACE + HOUR)
    w.drive.fail("get_change", ValueError("Graph 503"), lambda args: args["item_id"] == root_id)
    snapshot = dict(w.mirror.docs)
    w.reset_calls()
    counters = await w.run()
    assert counters["mirror_root_read_failed"] == 1 and not counters.get("mirror_root_lost")
    assert w.root.root_item_id == root_id and w.root.root_gone_at is not None
    assert w.mirror.docs == snapshot and w.drive.writes() == []


async def test_stray_tick_after_a_move_out_destroys_nothing():
    """The rollback (§27.9.12): the owner pauses the queue, moves `_Transcripts` out of the App Folder, deploys an
    old image, and moves it back after the roll-forward. A stray T2 tick in between only sees the mark."""
    w, a, b, gone = await world_with_archive()
    root_id = w.root.root_item_id
    snapshot = dict(w.mirror.docs)
    nodes = await root_leaves(w)
    for _ in range(3):
        w.advance(3 * HOUR)
        w.reset_calls()
        await w.run()
        assert w.drive.calls == [] and w.mirror.docs == snapshot
    paste(w, nodes)                                        # moved back in: the same item id
    await root_record(w)
    assert await w.mirror.clear_root_gone(USER, root_id)   # as the tick does when the record is written back
    c = await w.recording("c.mp4")
    await w.run()
    assert w.root.root_item_id == root_id and w.root.previous_root_ids == ()
    assert w.mirror_path(c) == "c.mp4.md" and w.mirror_path(a) == "Box/a.mp4.md"
    assert w.doc(gone).state == MIRROR_ARCHIVED and w.mirror.docs[w.doc(gone).key] == snapshot[w.doc(gone).key]


# -- a confirmed loss -------------------------------------------------------------------------------------------------

async def test_root_lost_after_grace_and_a_not_found_read():
    w, a, b, gone = await world_with_archive()
    old_root = w.root.root_item_id
    gone_key = w.doc(gone).content_key
    assert [d for d in w.mirror.docs.values() if d.kind == KIND_ARCHIVE]
    old = {n.item_id for n in await root_leaves(w)}
    w.advance(MIRROR_ROOT_GRACE + HOUR)
    w.reset_calls()
    counters = await w.run()
    assert len(root_reads(w, old_root)) == 1 and counters["mirror_root_lost"] == 1
    root = w.root
    assert root.root_item_id != old_root and old_root in root.previous_root_ids and root.root_gone_at is None
    assert root.root_name == ROOT_NAME and w.drive.nodes[root.root_item_id].parent_id == AREA
    assert w.last_host.root_marks == [(old_root, False)]
    # archive discarded: its documents and folders go, the pins with them
    assert w.doc(gone) is None and not [d for d in w.mirror.docs.values() if d.kind == KIND_ARCHIVE]
    assert not await w.mirror.pinned(USER, gone_key)
    # active re-created under the new root, from the store
    assert w.mirror_path(a) == "Box/a.mp4.md" and w.mirror_path(b) == "b.mp4.md"
    assert w.drive.under(w.doc(a).mirror_item_id, root.root_item_id)
    assert w.doc(a).state == MIRROR_ACTIVE and "hello there" in w.content(a)
    assert not written_ids(w) & old                        # nothing written to an item of the old root
    w.advance(WEEK + HOUR)
    await w.run()
    assert old_root in w.root.previous_root_ids            # kept


async def test_reset_resumes_after_a_crash_and_never_writes_under_the_old_root(monkeypatch):
    from src.services import drive_transcript_mirror as mod
    monkeypatch.setattr(mod, "RESET_PAGE", 1)
    w, a, b, gone = await world_with_archive()
    old_root = w.root.root_item_id
    old = {n.item_id for n in await root_leaves(w)}
    w.advance(MIRROR_ROOT_GRACE + HOUR)
    real = w.mirror.save_doc
    calls = {"n": 0}

    async def save_doc(doc):
        calls["n"] += 1
        if calls["n"] == 2:
            raise Crash("the process dies in the middle of the reset")
        await real(doc)

    monkeypatch.setattr(w.mirror, "save_doc", save_doc)
    w.reset_calls()
    with pytest.raises(Crash):
        await w.run()
    assert old_root in w.root.previous_root_ids and w.root.root_item_id == old_root     # the reset is under way
    assert w.drive.writes() == []
    monkeypatch.setattr(w.mirror, "save_doc", real)
    w.reset_calls()
    await w.run()
    assert w.root.root_item_id != old_root and old_root in w.root.previous_root_ids
    assert root_reads(w, old_root) == []                   # confirmed once: never read again
    assert w.mirror_path(a) == "Box/a.mp4.md" and w.mirror_path(b) == "b.mp4.md" and w.doc(gone) is None
    assert not written_ids(w) & old
    assert not [d for d in w.mirror.docs.values() if d.mirror_item_id in old or d.mirror_parent_id in old]


async def test_restored_old_root_stays_opaque_and_unwritten():
    w, a, b, gone = await world_with_archive()
    old_root = w.root.root_item_id
    nodes = await root_leaves(w)
    old = {n.item_id for n in nodes}
    before = {n.item_id: (n.name, n.parent_id, n.data) for n in nodes}
    w.advance(MIRROR_ROOT_GRACE + HOUR)
    await w.run()
    assert w.root.root_item_id != old_root
    paste(w, nodes)                                        # restored after the confirmation: a previous root now
    w.reset_calls()
    reported = 0
    for name in ("c.mp4", "d.mp4"):
        await w.recording(name)
        await w.delete_source(b) if w.rec(b) is not None else None
        w.advance(WEEK + HOUR)
        counters = await w.run()
        reported += counters.get("mirror_previous_roots_present", 0)
    assert reported == 1 and w.root.orphans_reported      # reported once, never adopted
    assert not written_ids(w) & old
    assert {n.item_id: (n.name, n.parent_id, n.data) for n in nodes} == before
    assert not [d for d in w.mirror.docs.values() if d.mirror_item_id in old or d.mirror_parent_id in old]
    roots = await w.mirror.roots(USER)
    assert old_root in {p for r in roots for p in r.previous_root_ids}        # the tick keeps it opaque (Δ205)
    assert old_root not in {r.root_item_id for r in roots}


async def test_previous_root_loss_triggers_nothing():
    w, a, b, gone = await world_with_archive()
    old_root = w.root.root_item_id
    nodes = await root_leaves(w)
    w.advance(MIRROR_ROOT_GRACE + HOUR)
    await w.run()
    new_root = w.root.root_item_id
    paste(w, nodes)                                        # restored, then deleted again by the owner
    cut(w, old_root)
    assert not await w.mirror.mark_root_gone(USER, old_root, w.clock)     # no document is the previous root's
    w.advance(MIRROR_ROOT_GRACE + HOUR)
    w.reset_calls()
    counters = await w.run()
    assert w.root.root_item_id == new_root and w.root.root_gone_at is None
    assert not counters.get("mirror_root_lost") and root_reads(w, old_root) == []
    assert w.mirror_path(a) == "Box/a.mp4.md"


async def test_root_renamed_or_moved_followed():
    w, a, b, gone = await world_with_archive()
    root_id = w.root.root_item_id
    mine = await w.folder("Mine")
    node = w.drive.nodes[root_id]
    node.name, node.parent_id = "Old transcripts", mine    # the owner renames the root and moves it into his folder
    await root_record(w, name="Old transcripts", parent=mine)
    c = await w.recording("c.mp4")
    w.reset_calls()
    await w.run()
    assert w.root.root_item_id == root_id and w.root.root_gone_at is None
    assert w.drive.path_of(w.doc(c).mirror_item_id) == "Mine/Old transcripts/c.mp4.md"
    w.advance(WEEK + HOUR)
    counters = await w.run()                               # the audit follows it too
    assert w.root.audited_at == w.clock and not counters.get("mirror_root_missing")
    assert not counters.get("mirror_orphan_roots") and w.root.previous_root_ids == ()
    assert w.mirror_path(a) == "Box/a.mp4.md"


async def test_root_listing_404_sets_the_mark_and_tells_the_tick():
    w, a, b, gone = await world_with_archive()
    root_id = w.root.root_item_id
    cut(w, root_id)                                        # gone, and the tick has not noticed yet
    w.advance(WEEK + HOUR)
    counters = await w.run()
    assert counters["mirror_root_missing"] == 1 and w.root.root_gone_at is not None
    assert w.last_host.root_marks == [(root_id, True)]
    assert w.root.root_item_id == root_id and w.doc(gone).state == MIRROR_ARCHIVED


# -- fix round 1 ------------------------------------------------------------------------------------------------------

async def test_detached_doc_keeps_its_pin_through_a_root_loss():
    from src.domain.drive_mirror import MIRROR_DETACHED, UNSTORED
    w, a, b, gone = await world_with_archive()
    await w.delete_source(b, why=UNSTORED)                 # still on the drive, no longer stored: detached
    w.advance(2 * HOUR)
    await w.run()
    assert w.doc(b).state == MIRROR_DETACHED
    key = w.doc(b).content_key
    await root_leaves(w)
    w.advance(MIRROR_ROOT_GRACE + HOUR)
    await w.run()
    doc = w.doc(b)
    assert doc is not None and doc.state == MIRROR_DETACHED and not doc.mirror_item_id
    assert await w.mirror.pinned(USER, key)                # a source that comes back never re-buys its audio


async def test_root_back_after_a_gap_longer_than_the_grace_destroys_nothing():
    """The rollback with the queue paused: no tick runs while the root is out, for longer than the grace. When it is
    back, its record is observed again before any mirror step, and the mark clears with no read and no destruction."""
    w, a, b, gone = await world_with_archive()
    root_id = w.root.root_item_id
    snapshot = dict(w.mirror.docs)
    nodes = await root_leaves(w)
    w.advance(3 * MIRROR_ROOT_GRACE)                       # no tick at all meanwhile
    paste(w, nodes)
    await root_record(w)                                   # the tick has not cleared the mark (yet)
    w.reset_calls()
    counters = await w.run()
    assert counters["mirror_root_mark_cleared"] == 1 and root_reads(w, root_id) == []
    assert w.root.root_item_id == root_id and w.root.previous_root_ids == () and w.root.root_gone_at is None
    assert w.drive.writes() == [] and w.mirror.docs == snapshot


async def test_confirmed_loss_waits_for_a_quota_hold_before_the_new_root():
    from dataclasses import replace
    w, a, b, gone = await world_with_archive()
    old_root = w.root.root_item_id
    await root_leaves(w)
    w.advance(MIRROR_ROOT_GRACE + HOUR)
    await w.mirror.save_root(replace(w.root, quota_hold_until=w.clock + 6 * HOUR))
    w.reset_calls()
    await w.run()
    assert old_root in w.root.previous_root_ids and w.root.root_item_id == old_root      # reset done, no new root
    assert not [c for c in w.drive.calls if c[0] in ("create_folder", "child_by_name")]
    w.advance(7 * HOUR)
    await w.run()
    assert w.root.root_item_id != old_root and w.mirror_path(a) == "Box/a.mp4.md"

"""Task 21C-3 (review Minor 1-2 of 21C-2; USER_DRIVE_INDEXING_RFC §27.9.5, Δ204): a skipped move-back PATCH spends no
op; a document with a pending gone mark whose way back is blocked is charged (back-off, then parked), not held for a
fixed time forever, and a backed-off or parked mark is not ready. Synthetic items only."""
from __future__ import annotations

from datetime import timedelta

from src.domain.drive_index import Scope
from src.domain.drive_index_params import MIRROR_FAILED_RETRY, MIRROR_MAX_ATTEMPTS
from src.domain.drive_index_planner import backoff

from .drive_mirror_fakes import AREA, DRIVE_IDENTITY, MirrorWorld, USER
from .test_drive_transcript_mirror_audit import folder_record, mirrored

HOUR = timedelta(hours=1)


async def test_a_skipped_move_back_patch_spends_no_op():
    w = MirrorWorld()
    outer = await w.folder("Outer")
    inner = await w.folder("Inner", outer)
    await mirrored(w, "a.mp4", parent=inner)
    mirror_outer = w.folder_doc(outer).mirror_item_id
    mirror_inner = w.folder_doc(inner).mirror_item_id
    w.drive.nodes[mirror_outer].parent_id = AREA
    await folder_record(w, mirror_outer, AREA, Scope.NORMAL, name="Outer")
    await folder_record(w, mirror_inner, mirror_outer, Scope.NORMAL, name="Inner")
    await w.recording("b.mp4", inner)
    w.reset_calls()
    counters = await w.run()
    moves = [c for c in w.drive.calls if c[0] == "move"]
    assert counters["mirror_moved_back"] == 1 and len(moves) == 1
    assert counters["mirror_ops"] == len(w.write_calls())           # every spent op was a drive write


async def _blocked_pruned_folder(w: MirrorWorld):
    folder = await w.folder("Old")
    a = await w.recording("a.mp4", folder)
    await w.run()
    mirror_folder = w.folder_doc(folder).mirror_item_id
    await w.delete_source(a)
    await w.delete_source(folder)
    w.drive.nodes[mirror_folder].parent_id = AREA
    await folder_record(w, mirror_folder, AREA, Scope.NORMAL, name="Old")
    w.drive.add_folder(w.root.root_item_id, "Old")                 # the way back is taken
    return folder, mirror_folder


async def test_a_blocked_way_back_backs_off_and_grows_then_parks():
    w = MirrorWorld()
    folder, mirror_folder = await _blocked_pruned_folder(w)
    w.advance(2 * HOUR)
    delays = []
    for n in range(1, MIRROR_MAX_ATTEMPTS):
        await w.run()
        doc = w.folder_doc(folder)
        assert doc.attempts == n and doc.failed_at is None
        assert doc.mark_ready_at == doc.retry_at == w.clock + backoff(n)
        delays.append(doc.retry_at - w.clock)
        w.advance(doc.retry_at - w.clock)
    assert delays == sorted(delays) and delays[-1] > delays[0]
    await w.run()                                                  # the last charge: parked
    doc = w.folder_doc(folder)
    assert doc.attempts == MIRROR_MAX_ATTEMPTS and doc.failed_at == w.clock
    assert doc.mark_ready_at == w.clock + MIRROR_FAILED_RETRY
    assert mirror_folder in w.drive.nodes


async def test_a_backed_off_or_parked_mark_is_not_ready_and_a_parked_one_is_quiet():
    w = MirrorWorld()
    folder, _ = await _blocked_pruned_folder(w)
    w.advance(2 * HOUR)
    await w.run()
    assert await w.mirror.ready_marks(USER, DRIVE_IDENTITY, w.clock, 10) == []     # backed off: no reserve for it
    for _ in range(MIRROR_MAX_ATTEMPTS - 1):
        w.advance(w.folder_doc(folder).retry_at - w.clock)
        await w.run()
    assert w.folder_doc(folder).failed_at is not None
    w.advance(MIRROR_FAILED_RETRY - HOUR)
    assert await w.mirror.ready_marks(USER, DRIVE_IDENTITY, w.clock, 10) == []     # parked: quiet for the whole week
    w.reset_calls()
    await w.run()
    assert w.write_calls() == [] and not [c for c in w.drive.calls if c[0] == "get_change"]
    w.advance(2 * HOUR)
    assert [d.key for d in await w.mirror.ready_marks(USER, DRIVE_IDENTITY, w.clock, 10)] == [w.folder_doc(folder).key]

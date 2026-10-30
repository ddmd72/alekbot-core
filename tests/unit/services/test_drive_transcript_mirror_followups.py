"""DriveTranscriptMirror follow-ups (plan Task 21C-2): the host's own stop exception, the empty move-back PATCH, and a
pruned folder outside the root whose way back is blocked. Synthetic items only."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Tuple, Type

import pytest

from src.domain.drive_index import Scope
from src.domain.drive_mirror_plan import MIRROR_PLAN_VERSION, RENDER_VERSION, seed_version

from .drive_mirror_fakes import AREA, HANDLE, FakeMirrorHost, MirrorWorld
from .test_drive_transcript_mirror_audit import folder_record, mirrored, writes_outside

HOUR = timedelta(hours=1)


class HostStop(Exception):
    """A stop signal only the host knows."""


@dataclass
class StopHost(FakeMirrorHost):
    stop_exceptions: Tuple[Type[BaseException], ...] = (HostStop,)


def test_seed_version_is_the_plan_and_render_versions():
    assert seed_version() == f"{MIRROR_PLAN_VERSION}.{RENDER_VERSION}"


@pytest.mark.parametrize("method", ["create_folder", "upload", "area_path"])
async def test_a_host_stop_exception_raised_by_a_drive_call_passes_through_unchanged(method):
    w = MirrorWorld()
    w.drive.fail(method, HostStop(), times=100)
    await w.recording("a.mp4")
    host = StopHost(w.repo, events=w.events, clock=w.clock)
    with pytest.raises(HostStop):
        await w.service.run(HANDLE, host)
    assert w.drive.faults[0].times == 99                    # raised once, and the step never called the drive again
    assert w.drive.calls[-1][0] == method
    assert not host.commits and w.mirror.docs == {}         # and nothing was written after it
    assert [e for e in w.events if e[0] == "save_doc"] == []


async def test_a_host_stop_exception_raised_by_a_commit_ends_the_run_and_writes_nothing_more():
    w = MirrorWorld()
    await w.run()
    await w.recording("a.mp4")
    seen = []

    class CommitStop(StopHost):
        async def commit(self, records):  # type: ignore[no-untyped-def]
            seen.append(len(records))
            raise HostStop()

    host = CommitStop(w.repo, events=w.events, clock=w.clock)
    w.reset_calls()
    with pytest.raises(HostStop):
        await w.service.run(HANDLE, host)
    assert len(seen) == 1


async def test_a_nested_moved_out_folder_is_moved_back_once_and_the_child_gets_no_empty_patch():
    w = MirrorWorld()
    outer = await w.folder("Outer")
    inner = await w.folder("Inner", outer)
    await mirrored(w, "a.mp4", parent=inner)
    mirror_outer = w.folder_doc(outer).mirror_item_id
    mirror_inner = w.folder_doc(inner).mirror_item_id
    w.drive.nodes[mirror_outer].parent_id = AREA          # the owner takes the outer mirror folder out ...
    await folder_record(w, mirror_outer, AREA, Scope.NORMAL, name="Outer")
    await folder_record(w, mirror_inner, mirror_outer, Scope.NORMAL, name="Inner")   # ... the child follows it
    b = await w.recording("b.mp4", inner)
    w.reset_calls()
    counters = await w.run()
    moves = [c for c in w.drive.calls if c[0] == "move"]
    assert moves and all(c[1].get("new_parent_id") or c[1].get("new_name") for c in moves)
    assert counters["mirror_moved_back"] == 1
    assert writes_outside(w, w.root.root_item_id) == []
    assert w.drive.nodes[mirror_outer].parent_id == w.root.root_item_id
    assert w.mirror_path(b) == "Outer/Inner/b.mp4.md"


async def test_a_pruned_folder_outside_the_root_with_a_blocked_way_back_is_tried_again_next_tick():
    w = MirrorWorld()
    folder = await w.folder("Old")
    a = await w.recording("a.mp4", folder)
    await w.run()
    mirror_folder = w.folder_doc(folder).mirror_item_id
    await w.delete_source(a)
    await w.delete_source(folder)
    w.drive.nodes[mirror_folder].parent_id = AREA          # the owner moved the (soon empty) mirror folder out ...
    await folder_record(w, mirror_folder, AREA, Scope.NORMAL, name="Old")
    blocker = w.drive.add_folder(w.root.root_item_id, "Old")   # ... and the way back is taken
    w.advance(2 * HOUR)
    await w.run()
    assert mirror_folder in w.drive.nodes and writes_outside(w, w.root.root_item_id) == []
    assert w.folder_doc(folder) is not None
    w.drive.remove(blocker)                                # the owner frees the name
    w.advance(2 * HOUR)
    await w.run()
    assert mirror_folder not in w.drive.nodes and w.folder_doc(folder) is None
    assert writes_outside(w, w.root.root_item_id) == []

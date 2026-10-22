"""Folder-delete cascade progress (USER_DRIVE_INDEXING_RFC §7.6, §7.8): a deleting folder whose remaining children
are deleting folders the current due page does not hold steps aside, so a wide deleted tree always finishes."""
import pytest
from .test_drive_indexing_service import World


async def _ticks_until_quiet(w, max_ticks):
    ticks = 0
    for _ in range(max_ticks):
        before = len(w.queue.tasks)
        await w.tick()
        ticks += 1
        cont = [t for t in w.queue.tasks[before:] if ":cont:" in (t["key"] or "")]
        if not cont:
            break
        w.clock.advance(max(1, max(t["delay"] for t in cont)))
    return ticks


class TestCascadeFinishes:

    @pytest.mark.slow
    async def test_a_wide_deleted_tree_is_removed(self):
        """150 subfolders, each holding one subfolder: more than one due page of folders whose children are all
        deleting folders. Each steps aside behind its children instead of stalling the class."""
        w = World(page_size=500)
        top = w.drive.add("Top", folder=True)
        mids = [w.drive.add(f"m{i:03d}", parent=top, folder=True) for i in range(150)]
        for i, m in enumerate(mids):
            w.drive.add(f"l{i:03d}", parent=m, folder=True)
        await w.settle()
        w.drive.delete(top)
        ticks = await _ticks_until_quiet(w, 30)
        assert w.repo.records == {} and w.repo.vectors == {}
        assert ticks < 30

    async def test_a_deep_deleted_chain_finishes_within_one_tick(self):
        """Children in the same due page are worked in that page, so a folder waits for them in the same tick."""
        w = World(page_size=500)
        parent = w.drive.add("Root", folder=True)
        for depth in range(20):
            parent = w.drive.add(f"d{depth:02d}", parent=parent, folder=True)
        w.drive.add("deep.pdf", parent=parent)
        await w.settle()
        w.drive.delete(next(iter(w.drive.items)))
        await w.tick()
        assert w.repo.records == {}

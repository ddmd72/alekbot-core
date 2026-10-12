"""Tick-level twins of the retrospective review fixes of Part A (retro_domain.md F1, F10)."""
import pytest

from src.domain.drive_index import DriveChange, DriveIndexRecord, Priority, RecordState, Scope
from src.services import drive_indexing_service as svc_mod

from .test_drive_indexing_service import World


class TestDeletedRepositoryStaysDeleted:
    """F1: the feed delivers the folder's tombstone before its `.git` tombstone (no post-order promise)."""

    @pytest.mark.parametrize("page_size", [50, 1])
    async def test_folder_tombstone_then_git_tombstone_removes_the_repository(self, page_size):
        w = World(page_size=page_size)
        site = w.drive.add("alek-site", folder=True)
        git = w.drive.add(".git", parent=site, folder=True)
        w.drive.add("a.py", parent=site)
        await w.settle()
        assert w.rec(site).scope is Scope.OPAQUE_ROOT and site in w.carded()
        for item_id in list(w.drive.items):
            if item_id in (site, git) or w.drive.items[item_id].parent_id == site:
                w.drive.items.pop(item_id)
        w.drive.log.append(DriveChange(item_id=site, deleted=True))
        w.drive.log.append(DriveChange(item_id=git, deleted=True))
        await w.settle()
        assert w.rec(site) is None, "a deleted repository folder came back as a live record"
        assert w.rec(git) is None
        assert site not in w.carded()


class TestSweepSpanningTicks:
    """F10: a record gone from the drive must not escape a sweep that spans ticks by being re-planned."""

    async def test_unresolved_ghost_resolved_mid_sweep_is_still_swept(self, monkeypatch):
        monkeypatch.setattr(svc_mod, "CASCADE_OPS_PER_TICK", 2)
        w = World()
        docs = w.drive.add("Docs", folder=True)
        w.drive.add("a.pdf", parent=docs)
        await w.settle()
        for i in range(3):   # stale records ahead of the ghost, so the sweep spans ticks
            w.repo.records[("u", f"g{i}")] = DriveIndexRecord(
                user_id="u", item_id=f"g{i}", name=f"g{i}.bin", parent_id=docs, kind="other",
                scope=Scope.NORMAL, state=RecordState.METADATA_ONLY, policy_version=1, seen_epoch=1)
        w.repo.records[("u", "zz-ghost")] = DriveIndexRecord(
            user_id="u", item_id="zz-ghost", name="ghost.pdf", parent_id=docs, kind="pdf",
            scope=Scope.UNRESOLVED, state=RecordState.UNRESOLVED, policy_version=1, seen_epoch=1,
            due_at=w.clock.now, priority=int(Priority.CASCADE))
        w.drive.resync_next = True   # 410: epoch 2 enumerates the drive, the ghost is not in it
        await w.settle()
        assert w.rec("zz-ghost") is None, "a record gone from the drive survived the sweep"
        assert all(w.rec(f"g{i}") is None for i in range(3))
        assert w.rec(docs) is not None

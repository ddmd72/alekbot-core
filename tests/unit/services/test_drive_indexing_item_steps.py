"""Per-item steps of the due loop (USER_DRIVE_INDEXING_RFC §3.4, §5.2, §7.6, §8.2): attempts per step, re-listing
only walkable folders, parked orphans, and the cascade budget."""
from dataclasses import replace
from datetime import timedelta

from src.domain import drive_index_planner as planner
from src.domain.drive_index import DriveChange, DriveIndexRecord, Priority, Reason, RecordState, Scope
from src.domain.drive_index_params import MAX_ATTEMPTS, RETRY_CAP
from src.services import drive_indexing_service as svc_mod

from .drive_index_fakes import ROOT
from .test_drive_indexing_service import World


async def _onboarded():
    w = World()
    w.drive.add("anchor.pdf")
    await w.settle()
    return w


def _drive_item(w, item_id, name, parent, folder=True, child_count=0):
    """An item the drive has but the feed never delivered (no log entry)."""
    w.drive.items[item_id] = DriveChange(item_id=item_id, name=name, parent_id=parent, is_folder=folder,
                                         child_count=child_count)


async def _seed_unresolved(w, item_id, name, parent_id, *, folder=True, child_count=0, due=True):
    rec = DriveIndexRecord(user_id="u", item_id=item_id, parent_id=parent_id, name=name, is_folder=folder,
                           child_count=child_count, kind="folder" if folder else "pdf", scope=Scope.UNRESOLVED,
                           state=RecordState.UNRESOLVED, policy_version=planner.POLICY_VERSION, seen_epoch=1,
                           due_at=w.clock.now if due else None, priority=int(Priority.CASCADE) if due else None)
    await w.repo.upsert_records([rec])


async def _ticks(w, n):
    for _ in range(n):
        await w.settle()
        w.clock.advance(RETRY_CAP.total_seconds())


class TestAttemptsResetAfterASuccessfulStep:

    async def test_a_probe_gets_its_own_attempts_after_a_slow_parent_lookup(self):
        """Two failed parent lookups must not use up the density probe's attempts that follow."""
        w = await _onboarded()
        _drive_item(w, "far", "far", ROOT)
        _drive_item(w, "big", "big", "far", child_count=600)
        await _seed_unresolved(w, "big", "big", "far", child_count=600)
        lookups = {"n": 0}
        real_get = w.drive.get_change

        async def flaky_get(user_id, item_id):
            if item_id == "far":
                lookups["n"] += 1
                if lookups["n"] <= 2:
                    raise ValueError("Graph 503 on the parent lookup")
            return await real_get(user_id, item_id)

        probes = []

        async def failing_list(user_id, folder_id, limit, cursor=None):
            probes.append(folder_id)
            raise ValueError("Graph 503 on the listing")

        w.drive.get_change = flaky_get
        w.drive.list_children_changes = failing_list
        await _ticks(w, 10)
        assert lookups["n"] == 3
        assert not w.rec("big").probe_pending and w.rec("big").scope is Scope.NORMAL
        assert probes.count("big") == MAX_ATTEMPTS

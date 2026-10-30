"""Per-item steps of the due loop (USER_DRIVE_INDEXING_RFC §3.4, §5.2, §7.6, §8.2): attempts per step, re-listing
only walkable folders, parked orphans, and the cascade budget."""
import pytest
from dataclasses import replace
from datetime import timedelta

from src.domain import drive_index_planner as planner
from src.domain.drive_index import CURRENT_VERSIONS, POLICY_VERSION, DriveChange, DriveIndexRecord, Priority, RecordState, Scope
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
    rec = DriveIndexRecord(user_id="u", connection_id="u", item_id=item_id, parent_id=parent_id, name=name, is_folder=folder,
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


class TestParentLookupExhaustionKeepsTheSubtree:

    async def test_transient_lookup_failures_keep_retrying_instead_of_deleting_the_subtree(self):
        w = await _onboarded()
        await _seed_unresolved(w, "orphan", "Projects", "far")
        await _seed_unresolved(w, "kid", "plan.pdf", "orphan", folder=False, due=False)
        lookups = []

        async def failing_get(user_id, item_id):
            lookups.append(item_id)
            raise ValueError("Graph 503 on the parent lookup")

        w.drive.get_change = failing_get
        await _ticks(w, MAX_ATTEMPTS + 3)
        assert lookups == ["far"] * (MAX_ATTEMPTS + 3)   # past MAX_ATTEMPTS: one lookup per RETRY_CAP
        orphan = w.rec("orphan")
        assert orphan is not None and orphan.state is RecordState.UNRESOLVED
        assert orphan.priority == int(Priority.RETRY) and orphan.due_at == w.clock.now   # due again: never parked
        assert w.rec("kid") is not None

    async def test_a_definite_not_found_still_removes_the_orphan_subtree(self):
        w = await _onboarded()
        await _seed_unresolved(w, "orphan", "Projects", "gone")     # the drive has no "gone": a deletion
        await _seed_unresolved(w, "kid", "plan.pdf", "orphan", folder=False, due=False)
        await _ticks(w, 2)
        assert w.rec("orphan") is None and w.rec("kid") is None


class TestRelistingOnlyWalkableFolders:

    @pytest.mark.slow
    async def test_policy_bump_over_a_repository_lists_no_opaque_folder(self, monkeypatch):
        w = World(page_size=200)
        w.drive.add("Notes.pdf")
        repo = w.drive.add("myproject", folder=True)
        w.drive.add("README.md", parent=repo)
        for d in range(10):
            sub = w.drive.add(f"pkg{d}", parent=repo, folder=True)
            inner = w.drive.add("inner", parent=sub, folder=True)
            for f in range(30):
                w.drive.add(f"mod{f}.py", parent=sub)
                w.drive.add(f"deep{f}.py", parent=inner)
        w.drive.add(".git", parent=repo, folder=True)
        await w.settle()
        assert w.rec(repo).scope is Scope.OPAQUE_ROOT
        # a bump that changes how folders are classified raises STRUCTURE_POLICY_SINCE with it (Ruling T-R5)
        monkeypatch.setattr(planner, "POLICY_VERSION", POLICY_VERSION + 1)
        monkeypatch.setattr(planner, "STRUCTURE_POLICY_SINCE", POLICY_VERSION + 1)
        monkeypatch.setattr(svc_mod, "CURRENT_VERSIONS", {**CURRENT_VERSIONS, "policy": POLICY_VERSION + 1})
        wasted = []
        real_list = w.drive.list_children_changes

        async def listing(user_id, folder_id, limit, cursor=None):
            if w.rec(folder_id).scope is not Scope.NORMAL:
                wasted.append(w.drive.items[folder_id].name)
            return await real_list(user_id, folder_id, limit, cursor)

        w.drive.list_children_changes = listing
        await w.settle(max_ticks=200)
        assert w.rec(repo).scope is Scope.OPAQUE_ROOT
        assert wasted == ["myproject"]   # only the README lead-text read lists the root (§5.2, ΔB8); nothing below it
        assert not [r for r in w.repo.records.values() if r.reobserve_pending]

    async def test_reobserve_step_skips_an_opaque_folder_without_a_call(self):
        w = await _onboarded()
        site = w.drive.add("site", folder=True)
        w.drive.add(".git", parent=site, folder=True)
        await w.settle()
        rec = w.rec(site)
        assert rec.scope is Scope.OPAQUE_ROOT
        await w.repo.upsert_records([replace(rec, reobserve_pending=True, reobserve_cursor="200",
                                             due_at=w.clock.now, priority=int(Priority.FIRST_SEEN))])
        listed = []
        real_list = w.drive.list_children_changes

        async def listing(user_id, folder_id, limit, cursor=None):
            listed.append(folder_id)
            return await real_list(user_id, folder_id, limit, cursor)

        w.drive.list_children_changes = listing
        await w.settle()
        assert listed == []
        assert not w.rec(site).reobserve_pending and w.rec(site).reobserve_cursor is None


class TestCascadeBudget:

    async def test_a_cascade_step_that_writes_nothing_costs_no_budget(self, monkeypatch):
        """A deleted folder whose children are folders already deleting writes nothing on its step; the budget it
        would have charged belongs to the steps that remove files."""
        w = await _onboarded()
        top = w.drive.add("Archive", folder=True)
        subs = [w.drive.add(f"y{i}", parent=top, folder=True) for i in range(3)]
        files = [w.drive.add(f"f{i}{j}.pdf", parent=s) for i, s in enumerate(subs) for j in range(2)]
        await w.settle()
        now = w.clock.now
        dying = [replace(w.rec(i), state=RecordState.DELETING, has_card=False, card_hash=None,
                         due_at=now - timedelta(seconds=10 - n), priority=int(Priority.CASCADE))
                 for n, i in enumerate([top, *subs])]
        await w.repo.upsert_records(dying)
        monkeypatch.setattr(svc_mod, "CASCADE_OPS_PER_TICK", 6)
        await w.tick()
        assert [f for f in files if w.rec(f) is not None] == []

"""Planner: a folder that turns opaque drops its pending re-listing (USER_DRIVE_INDEXING_RFC §5.2)."""
from datetime import datetime, timezone

from src.domain.connection_scope import ConnectionScope
from src.domain import drive_index_planner as planner
from src.domain.drive_index import DriveChange, DriveIndexRecord, EpochPhase, Reason, RecordState, Scope

ROOT = "root"
NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


class TestPlannerClearsRelistingOnOpaqueScope:

    @staticmethod
    def _ctx():
        return planner.PlanContext(scope=ConnectionScope(user_id="u", connection_id="u"), root_item_id=ROOT, epoch=1, phase=EpochPhase.IDLE, now=NOW,
                                   place_of=lambda lat, lon: None)

    def test_a_folder_rescoped_inside_an_opaque_root_drops_its_relisting(self):
        parent = DriveIndexRecord(user_id="u", connection_id="u", item_id="repo", parent_id=ROOT, name="repo", is_folder=True,
                                  scope=Scope.OPAQUE_ROOT, state=RecordState.METADATA_ONLY, reason=Reason.OPAQUE_ROOT,
                                  policy_version=planner.POLICY_VERSION)
        kid = DriveIndexRecord(user_id="u", connection_id="u", item_id="pkg", parent_id="repo", name="pkg", is_folder=True,
                               scope=Scope.NORMAL, state=RecordState.METADATA_ONLY, reason=Reason.FOLDER,
                               reobserve_pending=True, reobserve_cursor="200", policy_version=planner.POLICY_VERSION)
        plan = planner.plan_observation(planner.change_from_record(kid), kid, parent, None, self._ctx(), parent,
                                        observed=False)
        assert plan.upsert.scope is Scope.INSIDE_OPAQUE
        assert not plan.upsert.reobserve_pending and plan.upsert.reobserve_cursor is None

    def test_a_marker_child_closing_its_folder_drops_the_folders_relisting(self):
        folder = DriveIndexRecord(user_id="u", connection_id="u", item_id="site", parent_id=ROOT, name="site", is_folder=True,
                                  scope=Scope.NORMAL, state=RecordState.METADATA_ONLY, reason=Reason.FOLDER,
                                  reobserve_pending=True, reobserve_cursor="200",
                                  policy_version=planner.POLICY_VERSION)
        git = DriveChange(item_id="git", parent_id="site", name=".git", is_folder=True)
        plan = planner.plan_observation(git, None, folder, None, self._ctx())
        closed = [u for u in plan.parent_updates if u.item_id == "site"]
        assert closed and closed[0].scope is Scope.OPAQUE_ROOT
        assert not closed[0].reobserve_pending and closed[0].reobserve_cursor is None

    def test_an_unresolved_folder_keeps_its_relisting_until_its_probe(self):
        """A reopened big folder waits for its density probe: if it turns out walkable, its subtree is listed."""
        folder = DriveIndexRecord(user_id="u", connection_id="u", item_id="big", parent_id=ROOT, name="big", is_folder=True,
                                  child_count=600, scope=Scope.UNRESOLVED, state=RecordState.UNRESOLVED,
                                  probe_pending=True, reobserve_pending=True, policy_version=planner.POLICY_VERSION)
        plan = planner.plan_observation(planner.change_from_record(folder), folder, None, None, self._ctx(), None,
                                        observed=False)
        assert plan.upsert.reobserve_pending

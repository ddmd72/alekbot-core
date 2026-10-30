"""Planner: tombstone order of a dying repository, the density anchor, and re-plans that keep their epoch."""
from dataclasses import replace
from datetime import datetime, timezone

from src.domain.connection_scope import ConnectionScope
from src.domain.drive_index import (
    POLICY_VERSION, ContentClass, DriveChange, DriveIndexRecord, EpochPhase, RecordState, Reason, Scope,
)
from src.domain.drive_index_planner import PlanContext, change_from_record, needs_probe, plan_observation

NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
ROOT = "root"


def ctx(epoch=1, phase=EpochPhase.IDLE):
    return PlanContext(scope=ConnectionScope(user_id="u", connection_id="u"), root_item_id=ROOT, epoch=epoch, phase=phase, now=NOW,
                       place_of=lambda lat, lon: None)


def folder_rec(item_id, name, scope=Scope.NORMAL, reason=Reason.FOLDER, parent=ROOT, **kw):
    return DriveIndexRecord(user_id="u", connection_id="u", item_id=item_id, name=name, parent_id=parent, is_folder=True,
                            kind="folder", content_class=ContentClass.FOLDER, scope=scope,
                            state=RecordState.METADATA_ONLY, reason=reason, policy_version=POLICY_VERSION, **kw)


class TestDyingRepositoryIsNotReopened:
    """A marker tombstone that follows its folder's tombstone must not revive the folder (§5.2, §7.6)."""

    def test_marker_tombstone_after_its_folder_tombstone_does_not_revive_the_folder(self):
        site = folder_rec("p", "alek-site", scope=Scope.OPAQUE_ROOT, reason=Reason.OPAQUE_ROOT)
        dying = plan_observation(DriveChange(item_id="p", deleted=True), site, None, None, ctx()).upsert
        assert dying.state is RecordState.DELETING
        git = folder_rec("g", ".git", scope=Scope.INSIDE_OPAQUE, parent="p")
        plan = plan_observation(DriveChange(item_id="g", deleted=True), git, None, None, ctx(), dying)
        assert plan.parent_updates == ()

    def test_pyvenv_cfg_tombstone_after_its_folder_tombstone_does_not_revive_the_folder(self):
        venv = folder_rec("p", "env", scope=Scope.OPAQUE_ROOT, reason=Reason.OPAQUE_ROOT)
        dying = plan_observation(DriveChange(item_id="p", deleted=True), venv, None, None, ctx()).upsert
        cfg = DriveIndexRecord(user_id="u", connection_id="u", item_id="c", name="pyvenv.cfg", parent_id="p",
                               scope=Scope.INSIDE_OPAQUE, state=RecordState.METADATA_ONLY)
        plan = plan_observation(DriveChange(item_id="c", deleted=True), cfg, None, None, ctx(), dying)
        assert plan.parent_updates == () and plan.delete_item_id == "c"


class TestDensityAnchor:
    """A size-only re-decision clears the hysteresis anchor, so a refilled dense tree is probed again (§3.6)."""

    def test_dense_tree_cleaned_below_half_then_refilled_is_probed_again(self):
        dense = folder_rec("c", "cache", scope=Scope.OPAQUE_ROOT, reason=Reason.DENSE_TREE,
                           child_count=700, probed_child_count=700)
        cleaned = plan_observation(DriveChange(item_id="c", name="cache", parent_id=ROOT, is_folder=True,
                                               child_count=300), dense, None, None, ctx()).upsert
        assert cleaned.scope is Scope.NORMAL and cleaned.probed_child_count == 0
        refilled = DriveChange(item_id="c", name="cache", parent_id=ROOT, is_folder=True, child_count=800)
        assert needs_probe(refilled, Scope.NORMAL, cleaned) is True

    def test_anchor_is_kept_while_the_density_decision_stands(self):
        dense = folder_rec("c", "cache", scope=Scope.OPAQUE_ROOT, reason=Reason.DENSE_TREE,
                           child_count=700, probed_child_count=700)
        grown = plan_observation(DriveChange(item_id="c", name="cache", parent_id=ROOT, is_folder=True,
                                             child_count=900), dense, None, None, ctx()).upsert
        assert grown.probed_child_count == 700 and grown.reason is Reason.DENSE_TREE


class TestReplanKeepsSeenEpoch:
    """Re-planning a stored record is not an observation; only observations stamp seen_epoch (§7.5)."""

    def test_replan_of_stored_record_keeps_seen_epoch(self):
        parent = folder_rec("p", "Docs", seen_epoch=2)
        ghost = DriveIndexRecord(user_id="u", connection_id="u", item_id="z", name="ghost.pdf", parent_id="p", kind="pdf",
                                 scope=Scope.UNRESOLVED, state=RecordState.UNRESOLVED, seen_epoch=1)
        plan = plan_observation(change_from_record(ghost), ghost, parent, None, ctx(epoch=2, phase=EpochPhase.SWEEPING),
                                parent, observed=False)
        assert plan.upsert.scope is Scope.NORMAL and plan.upsert.seen_epoch == 1

    def test_unchanged_replan_while_enumerating_does_not_stamp(self):
        # the stored record is what a plan builds, so the re-plan takes the unchanged branch, not _build
        seen = DriveChange(item_id="f", name="Docs", parent_id=ROOT, is_folder=True, child_count=3)
        stored = plan_observation(seen, None, None, None, ctx(epoch=1, phase=EpochPhase.ENUMERATING)).upsert
        assert stored.seen_epoch == 1
        plan = plan_observation(change_from_record(stored), stored, None, None,
                                ctx(epoch=2, phase=EpochPhase.ENUMERATING), observed=False)
        assert plan.upsert is None, "a re-plan of an unchanged record stamped it as seen"

    def test_unchanged_observation_while_enumerating_still_stamps(self):
        seen = DriveChange(item_id="f", name="Docs", parent_id=ROOT, is_folder=True, child_count=3)
        stored = plan_observation(seen, None, None, None, ctx(epoch=1, phase=EpochPhase.ENUMERATING)).upsert
        plan = plan_observation(change_from_record(stored), stored, None, None,
                                ctx(epoch=2, phase=EpochPhase.ENUMERATING))
        assert plan.upsert == replace(stored, seen_epoch=2)   # only the stamp changed: the unchanged branch

    def test_observation_still_stamps(self):
        parent = folder_rec("p", "Docs", seen_epoch=2)
        ghost = DriveIndexRecord(user_id="u", connection_id="u", item_id="z", name="ghost.pdf", parent_id="p", kind="pdf",
                                 scope=Scope.UNRESOLVED, state=RecordState.UNRESOLVED, seen_epoch=1)
        plan = plan_observation(replace(change_from_record(ghost)), ghost, parent, None,
                                ctx(epoch=2, phase=EpochPhase.SWEEPING), parent)
        assert plan.upsert.seen_epoch == 2

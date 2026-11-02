"""The planner on a filtered scope (MULTI_DRIVE_RFC §11.3, E20): scope roots, the whole drive's parentless items, skeleton
ancestors and the scope restart."""
from dataclasses import replace
from datetime import datetime, timezone

from src.domain.connection_scope import ConnectionScope
from src.domain.drive_card import card_eligible
from src.domain.drive_index import DriveChange, DriveSyncState, EpochPhase, Reason, RecordState, Scope
from src.domain.drive_index_planner import (
    PlanContext, parent_scope, plan_observation, scope_ancestor_classification, scope_restart,
)

NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
ROOT, RD, PROJECTS = "root", "f-rd", "f-projects"


def ctx(**kw):
    base = dict(scope=ConnectionScope(user_id="usr-1", connection_id="conn-1"), root_item_id=ROOT, epoch=1,
                phase=EpochPhase.IDLE, now=NOW, place_of=lambda lat, lon: None, filtered=True,
                scope_root_ids=frozenset({RD}), scope_ancestor_ids=frozenset({PROJECTS}))
    base.update(kw)
    return PlanContext(**base)


def ch(item_id, name, parent, folder=False):
    return DriveChange(item_id=item_id, name=name, parent_id=parent, is_folder=folder)


def test_a_child_of_a_scope_root_is_normal_and_the_root_no_longer_is_when_filtered():
    assert parent_scope(RD, None, ROOT, scope_root_ids=frozenset({RD})) is Scope.NORMAL
    assert parent_scope(ROOT, None, ROOT, root_covered=False) is None
    assert parent_scope(ROOT, None, ROOT) is Scope.NORMAL           # today's rule, unchanged


def test_an_empty_parent_is_normal_only_with_the_whole_drive():
    assert parent_scope("", None, ROOT, whole_drive=True) is Scope.NORMAL
    assert parent_scope("", None, ROOT) is None


def test_a_scope_folder_is_classified_normal_though_its_parent_has_no_record():
    plan = plan_observation(ch(RD, "R&D", ROOT, folder=True), None, None, None, ctx())
    assert plan.upsert is not None
    assert (plan.upsert.scope, plan.upsert.reason) == (Scope.NORMAL, Reason.FOLDER)


def test_a_file_under_a_scope_folder_is_classified_from_the_stored_folder():
    folder = plan_observation(ch(RD, "R&D", ROOT, folder=True), None, None, None, ctx()).upsert
    plan = plan_observation(ch("plan", "Tom's plan.docx", RD), None, folder, None, ctx())
    assert plan.upsert is not None and plan.upsert.scope is Scope.NORMAL


def test_an_ancestor_of_a_scope_folder_is_a_skeleton_and_never_card_eligible():
    plan = plan_observation(ch(PROJECTS, "Projects", ROOT, folder=True), None, None, None, ctx())
    rec = plan.upsert
    assert rec is not None
    assert (rec.scope, rec.state, rec.reason) == (Scope.NORMAL, RecordState.METADATA_ONLY, Reason.SCOPE_ANCESTOR)
    assert not card_eligible(rec)
    assert not rec.reobserve_pending and not rec.probe_pending


def test_the_skeleton_classification_is_for_a_folder():
    cls = scope_ancestor_classification(ch(PROJECTS, "Projects", ROOT, folder=True))
    assert cls.stored and cls.reason is Reason.SCOPE_ANCESTOR


def test_a_selected_folder_that_is_also_on_a_path_is_not_a_skeleton():
    c = ctx(scope_ancestor_ids=frozenset({RD}))
    rec = plan_observation(ch(RD, "R&D", ROOT, folder=True), None, None, None, c).upsert
    assert rec is not None and rec.reason is Reason.FOLDER


def test_scope_restart_enumerates_again_under_the_new_key():
    state = DriveSyncState(user_id="usr-1", connection_id="conn-1", cursor="c9", epoch=3,
                           epoch_phase=EpochPhase.IDLE, round_complete=True, round_started_at=NOW)
    out = scope_restart(state, "abc123")
    assert (out.cursor, out.epoch, out.epoch_phase, out.round_complete, out.applied_scope) == (
        None, 4, EpochPhase.ENUMERATING, False, "abc123")
    assert replace(out, cursor=state.cursor, epoch=3, epoch_phase=EpochPhase.IDLE, round_complete=True,
                   round_started_at=NOW, applied_scope="") == state

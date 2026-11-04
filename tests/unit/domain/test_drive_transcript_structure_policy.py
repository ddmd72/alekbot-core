"""A POLICY_VERSION bump that changes media admission only (policy 3, §27.3) leaves structure decisions made at or after
STRUCTURE_POLICY_SINCE standing (Ruling T-R5); a record older than the floor is re-decided. The floor is policy 5 since
config files changed the density probe's class (Δ248), so a policy-2 record is now re-decided too. Also: a pending
media record is never "unread" while transcription is off (§27.3)."""
from datetime import datetime, timezone

import pytest

from src.domain.connection_scope import ConnectionScope
from src.domain import drive_index_planner as planner
from src.domain import drive_index_policy as pol
from src.domain.drive_index import (
    POLICY_VERSION, STRUCTURE_POLICY_SINCE, ContentClass, DriveChange, DriveIndexRecord, EpochPhase, Reason,
    RecordState, Scope,
)
from src.domain.drive_index_planner import PlanContext, change_from_record, needs_probe, plan_observation
from src.domain.drive_index_policy import SLICE2_CONTENT_KINDS
from src.domain.drive_search import is_not_read_yet, is_unread

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)


def ctx():
    return PlanContext(scope=ConnectionScope("u", "u"), root_item_id="root", epoch=1, phase=EpochPhase.IDLE, now=NOW,
                       place_of=lambda lat, lon: None)


def folder(policy, scope=Scope.OPAQUE_ROOT, reason=Reason.DENSE_TREE, **kw):
    base = dict(user_id="u", item_id="f1", parent_id="root", name="dump", is_folder=True, kind="folder",
                content_class=ContentClass.FOLDER, scope=scope, state=RecordState.METADATA_ONLY, reason=reason,
                child_count=900, probed_child_count=900, policy_version=policy)
    base.update(kw)
    return DriveIndexRecord(**base)


@pytest.fixture
def later_bump(monkeypatch):
    """A later, media-only POLICY_VERSION bump: the planner's current policy is one above the structure floor, so a
    record stored at the floor is behind the current policy (as a policy-2 record was under policy 4)."""
    monkeypatch.setattr(planner, "POLICY_VERSION", STRUCTURE_POLICY_SINCE + 1)
    return STRUCTURE_POLICY_SINCE


def test_structure_floor_is_policy_5_and_not_above_the_current_policy():
    # 5 raised the floor from 2: config (YAML, TOML, INI) became a document in the density probe (Δ248)
    assert STRUCTURE_POLICY_SINCE == 5 and POLICY_VERSION >= STRUCTURE_POLICY_SINCE


def test_floor_collapsed_folder_readme_not_re_read_after_a_media_only_bump(monkeypatch, later_bump):
    monkeypatch.setattr(pol, "ADMITTED_KINDS", SLICE2_CONTENT_KINDS)
    rec = folder(later_bump, name=".config", reason=Reason.DOT_NAME, probed_child_count=0, readme_excerpt="Lead.", readme_version="c1", readme_item_id="r1")
    again = plan_observation(change_from_record(rec), rec, None, None, ctx()).upsert
    assert not again.readme_pending and again.readme_excerpt == "Lead."


def test_policy_1_collapsed_folder_readme_is_re_read(monkeypatch):
    monkeypatch.setattr(pol, "ADMITTED_KINDS", SLICE2_CONTENT_KINDS)
    rec = folder(1, name=".config", reason=Reason.DOT_NAME, probed_child_count=0, readme_excerpt="Lead.", readme_version="c1", readme_item_id="r1")
    again = plan_observation(change_from_record(rec), rec, None, None, ctx()).upsert
    assert again.readme_pending


def _grown(rec, count):
    return DriveChange(item_id=rec.item_id, name=rec.name, parent_id="root", is_folder=True, child_count=count)


def test_floor_dense_folder_is_not_re_probed_after_a_media_only_bump(later_bump):
    assert needs_probe(_grown(folder(later_bump), 700), Scope.NORMAL, folder(later_bump)) is False


def test_policy_1_dense_folder_is_re_probed():
    assert needs_probe(_grown(folder(1), 700), Scope.NORMAL, folder(1)) is True


def test_floor_marker_opaque_folder_is_not_re_planned_after_a_media_only_bump(later_bump):
    marker = folder(later_bump, reason=Reason.OPAQUE_ROOT, probed_child_count=0, child_count=5000)
    assert needs_probe(_grown(marker, 5000), Scope.NORMAL, marker) is False


def test_policy_1_marker_opaque_folder_is_re_planned():
    marker = folder(1, reason=Reason.OPAQUE_ROOT, probed_child_count=0, child_count=5000)
    assert needs_probe(_grown(marker, 5000), Scope.NORMAL, marker) is True


@pytest.mark.parametrize("kind", ["audio", "video"])
def test_pending_media_is_not_unread_while_transcription_is_off(kind):
    rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="m", kind=kind, state=RecordState.PENDING)
    # off = the media set is not given (Task 5: the bare policy default now counts recordings, G2)
    kinds, images = pol.ADMITTED_KINDS, pol.TRIAGED_KINDS
    assert not is_unread(rec, kinds, images) and not is_not_read_yet(rec, kinds, images)

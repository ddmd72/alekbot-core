"""Policy 3 changes media admission only: structure decisions made under policy 2 stand (STRUCTURE_POLICY_SINCE, Ruling
T-R5); a policy-1 record is still re-decided. Also: a pending media record is never "unread" while transcription is
off (§27.3)."""
from datetime import datetime, timezone

import pytest

from src.domain.connection_scope import ConnectionScope
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


def test_structure_floor_is_below_the_current_policy():
    assert STRUCTURE_POLICY_SINCE == 2 and POLICY_VERSION > STRUCTURE_POLICY_SINCE


def test_policy_2_collapsed_folder_readme_not_re_read(monkeypatch):
    monkeypatch.setattr(pol, "ADMITTED_KINDS", SLICE2_CONTENT_KINDS)
    rec = folder(2, name=".config", reason=Reason.DOT_NAME, probed_child_count=0, readme_excerpt="Lead.", readme_version="c1", readme_item_id="r1")
    again = plan_observation(change_from_record(rec), rec, None, None, ctx()).upsert
    assert not again.readme_pending and again.readme_excerpt == "Lead."


def test_policy_1_collapsed_folder_readme_is_re_read(monkeypatch):
    monkeypatch.setattr(pol, "ADMITTED_KINDS", SLICE2_CONTENT_KINDS)
    rec = folder(1, name=".config", reason=Reason.DOT_NAME, probed_child_count=0, readme_excerpt="Lead.", readme_version="c1", readme_item_id="r1")
    again = plan_observation(change_from_record(rec), rec, None, None, ctx()).upsert
    assert again.readme_pending


def _grown(rec, count):
    return DriveChange(item_id=rec.item_id, name=rec.name, parent_id="root", is_folder=True, child_count=count)


def test_policy_2_dense_folder_is_not_re_probed():
    assert needs_probe(_grown(folder(2), 700), Scope.NORMAL, folder(2)) is False


def test_policy_1_dense_folder_is_re_probed():
    assert needs_probe(_grown(folder(1), 700), Scope.NORMAL, folder(1)) is True


def test_policy_2_marker_opaque_folder_is_not_re_planned():
    marker = folder(2, reason=Reason.OPAQUE_ROOT, probed_child_count=0, child_count=5000)
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

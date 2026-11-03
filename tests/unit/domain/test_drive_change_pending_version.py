"""A pending change key (GOOGLE_DRIVE_RFC GE-36): `with_stored_version` takes the stored record's key when the record
has the change's modified time and keeps the change pending otherwise; the planner refuses a change that is still
pending, live or deleted, and plans the same change once resolved. Technical ids only."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.domain.connection_scope import ConnectionScope
from src.domain.drive_index import DriveChange, DriveIndexRecord, EpochPhase, with_stored_version
from src.domain.drive_index_planner import PlanContext, plan_observation

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 1, 2, tzinfo=timezone.utc)
ROOT = "root"
PENDING = DriveChange(item_id="f-1", parent_id=ROOT, name="Tom's plan", mime_type="text/markdown",
                      content_version=f"m:{T0.isoformat()}", modified_at=T0, content_version_pending=True)


def _stored(**kw) -> DriveIndexRecord:
    fields = dict(user_id="u", connection_id="c", item_id="f-1", parent_id=ROOT, name="Tom's plan",
                  content_version="r:rev-3", modified_at=T0)
    fields.update(kw)
    return DriveIndexRecord(**fields)


def _ctx() -> PlanContext:
    return PlanContext(scope=ConnectionScope(user_id="u", connection_id="c"), root_item_id=ROOT, epoch=1,
                       phase=EpochPhase.IDLE, now=T1, place_of=lambda lat, lon: None)


def test_a_new_change_field_defaults_to_final():
    assert DriveChange(item_id="f-1").content_version_pending is False


def test_the_same_modified_time_takes_the_stored_key_without_a_call():
    out = with_stored_version(PENDING, _stored())
    assert out.content_version == "r:rev-3" and out.content_version_pending is False
    assert replace(out, content_version=PENDING.content_version, content_version_pending=True) == PENDING


@pytest.mark.parametrize("stored", [
    _stored(modified_at=T1),                       # edited or renamed since: the key must be read
    _stored(content_version=""),                   # a record with no key proves nothing
    _stored(modified_at=None),
    None,                                          # a new item
], ids=["other-time", "empty-key", "record-without-time", "no-record"])
def test_anything_else_still_needs_a_call(stored):
    assert with_stored_version(PENDING, stored) == PENDING


def test_a_change_without_a_modified_time_still_needs_a_call():
    change = replace(PENDING, modified_at=None)
    assert with_stored_version(change, _stored(modified_at=None)) == change


def test_a_final_change_is_never_rewritten():
    final = replace(PENDING, content_version="r:rev-9", content_version_pending=False)
    assert with_stored_version(final, _stored()) is final


@pytest.mark.parametrize("change", [PENDING, replace(PENDING, deleted=True)], ids=["live", "deleted"])
def test_the_planner_refuses_a_pending_change(change):
    with pytest.raises(ValueError, match="pending"):
        plan_observation(change, None, None, None, _ctx())


def test_the_planner_stores_the_resolved_key():
    resolved = replace(PENDING, content_version="r:rev-3", content_version_pending=False)
    plan = plan_observation(resolved, None, None, None, _ctx())
    assert plan.upsert is not None and plan.upsert.content_version == "r:rev-3"

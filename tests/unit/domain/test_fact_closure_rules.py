"""The closure and Cabinet-invalidation rules of `domain/fact_lifecycle.py` (S4 T2c, hexagonal review H1): one place for
the fields, the trail reason and the "rewritten after the page loaded" refusal the repository and the dry-run kit used to
write each on their own."""
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.entities import FactEntity, FactState, FactType
from src.domain.exceptions import FactResolutionError
from src.domain.fact_lifecycle import LIFECYCLE_EVENTS_CAP, cabinet_invalidation, superseded_closure

NOW = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)
EARLIER = NOW - timedelta(days=3)


def _fact(fact_id: str, text: str = "lives in Valencia", lineage: str = "L", **over) -> FactEntity:
    fields = dict(id=fact_id, account_id="acc", created_by_user_id="usr", lineage_id=lineage, text=text,
                  type=FactType.STATE, created_at=EARLIER, valid_from=EARLIER, last_updated=EARLIER)
    fields.update(over)
    return FactEntity(**fields)


OLD_EVENT = {"at": EARLIER, "from": None, "to": "current", "actor": "stage1", "reason": "created"}


# ------------------------------------------------------------------------------------------ superseded_closure

def test_a_version_closes_the_old_document_with_replaced_by():
    old = _fact("f-v1", lifecycle_events=[OLD_EVENT])
    new = _fact("f-v2", version=2)

    fields = superseded_closure(old, new, NOW, "stage1")

    assert fields == {
        "state": FactState.SUPERSEDED, "is_current": False, "valid_to": NOW, "superseded_by": "f-v2", "last_updated": NOW,
        "lifecycle_events": [OLD_EVENT, {"at": NOW, "from": "current", "to": "superseded", "actor": "stage1",
                                         "reason": "replaced by f-v2"}],
    }


def test_a_merge_across_lineages_says_merged_into_and_keeps_the_from_state():
    stale = _fact("f-a", lineage="LA", state=FactState.STALE)
    merged = _fact("f-m", lineage="LM")

    fields = superseded_closure(stale, merged, NOW, "janitor")

    assert fields["superseded_by"] == "f-m" and fields["state"] is FactState.SUPERSEDED
    assert fields["lifecycle_events"] == [
        {"at": NOW, "from": "stale", "to": "superseded", "actor": "janitor", "reason": "merged into f-m"}
    ]


def test_the_closure_keeps_only_the_newest_events_and_does_not_touch_the_stored_trail():
    trail = [{**OLD_EVENT, "reason": f"r{i}"} for i in range(LIFECYCLE_EVENTS_CAP)]
    old = _fact("f-v1", lifecycle_events=trail)

    fields = superseded_closure(old, _fact("f-v2"), NOW, "stage2a")

    events = fields["lifecycle_events"]
    assert len(events) == LIFECYCLE_EVENTS_CAP and events[0]["reason"] == "r1" and events[-1]["reason"] == "replaced by f-v2"
    assert len(old.lifecycle_events) == LIFECYCLE_EVENTS_CAP and old.lifecycle_events[-1]["reason"] == f"r{LIFECYCLE_EVENTS_CAP - 1}"


# ------------------------------------------------------------------------------------------ cabinet_invalidation

def test_the_requested_fact_itself_is_invalidated_with_the_cabinet_trail_entry():
    stored = _fact("f-x", state=FactState.ARCHIVED, lifecycle_events=[OLD_EVENT])

    fields = cabinet_invalidation("lives in Valencia", stored, "f-x", NOW)

    assert fields == {
        "state": FactState.INVALIDATED, "is_current": True, "valid_to": NOW, "last_updated": NOW,
        "lifecycle_events": [OLD_EVENT, {"at": NOW, "from": "archived", "to": "invalidated", "actor": "cabinet",
                                         "reason": "deleted in the Cabinet"}],
    }


def test_the_requested_id_itself_passes_whatever_text_the_page_showed():
    fields = cabinet_invalidation("something the page never had", _fact("f-x"), "f-x", NOW)

    assert fields["state"] is FactState.INVALIDATED


def test_a_followed_version_with_the_text_the_page_showed_is_invalidated():
    fields = cabinet_invalidation("lives in Valencia", _fact("f-v2", version=2), "f-v1", NOW)

    assert fields["state"] is FactState.INVALIDATED
    assert fields["lifecycle_events"][-1] == {"at": NOW, "from": "current", "to": "invalidated", "actor": "cabinet",
                                              "reason": "deleted in the Cabinet"}


def test_a_followed_version_with_a_newer_text_is_refused_naming_it():
    with pytest.raises(FactResolutionError) as caught:
        cabinet_invalidation("lives in Valencia", _fact("f-v2", text="lives in Puçol", version=2), "f-v1", NOW)

    assert str(caught.value) == "fact f-v1 was rewritten after the page loaded; reload"
    assert caught.value.current_id == "f-v2" and not isinstance(caught.value, ValueError)

"""Pin, lifecycle events, the new FactEntity fields and the extended in-place set (Facts S3, plan §3.7, §3.8, T1)."""

from datetime import datetime, timezone

import pytest

from src.domain.entities import (
    ContextPriority,
    ExpiryKind,
    FactDomain,
    FactEntity,
    FactState,
    FactType,
    normalize_fact_taxonomy,
)
from src.domain.exceptions import FactExpiryError
from src.domain.fact_expiry import EXPIRY_NONE, NOTHING_WRITTEN, Expiry
from src.domain.fact_lifecycle import (
    IN_PLACE_FIELDS,
    LIFECYCLE_EVENTS_CAP,
    check_pin,
    closure_fields,
    expiry_pinned,
    lifecycle_event,
    with_event,
)

NOW = datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc)
HORIZON = Expiry(ExpiryKind.HORIZON, days=14)


def fact(**overrides):
    return FactEntity(account_id="a", created_by_user_id="u", lineage_id="l", text="t", type=FactType.EVENT, **overrides)


# --- pin -------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "domain, type_, tags",
    [
        (FactDomain.AGENT_DIRECTIVE, FactType.EVENT, []),
        (FactDomain.WORK, FactType.PRINCIPLE, []),
        (None, FactType.EVENT, ["mindset"]),
        (FactDomain.WORK, FactType.EVENT, ["Mindset", "x"]),
    ],
)
def test_each_pin_trigger_pins(domain, type_, tags):
    assert expiry_pinned(domain, type_, tags) is True


@pytest.mark.parametrize(
    "domain, type_, tags",
    [(None, FactType.EVENT, []), (FactDomain.PREFERENCE, FactType.STATE, ["habit"]), (FactDomain.WORK, FactType.SYSTEM, ["mind"])],
)
def test_other_facts_are_not_pinned(domain, type_, tags):
    assert expiry_pinned(domain, type_, tags) is False


def test_a_pinned_fact_without_a_decision_gets_none_and_with_none_keeps_it():
    assert check_pin(None, True) is EXPIRY_NONE
    assert check_pin(EXPIRY_NONE, True) is EXPIRY_NONE


@pytest.mark.parametrize("expiry", [HORIZON, Expiry(ExpiryKind.DATE, on=datetime(2027, 1, 1).date())])
def test_a_pinned_fact_refuses_a_date_or_a_horizon(expiry):
    with pytest.raises(FactExpiryError) as refused:
        check_pin(expiry, True)
    assert "directives, principles and mindset facts take {kind: none}" in str(refused.value)
    assert str(refused.value).endswith(NOTHING_WRITTEN)


def test_an_unpinned_fact_keeps_its_decision_and_needs_one():
    assert check_pin(HORIZON, False) is HORIZON
    with pytest.raises(FactExpiryError, match="required"):
        check_pin(None, False)


# --- events ----------------------------------------------------------------------------------------------------------


def test_an_event_has_the_rfc_keys_and_state_values():
    assert lifecycle_event(NOW, FactState.CURRENT, FactState.STALE, "janitor", "expired 2026-10-23") == {
        "at": NOW, "from": "current", "to": "stale", "actor": "janitor", "reason": "expired 2026-10-23",
    }


def test_the_first_event_of_a_fact_has_no_from_state():
    assert lifecycle_event(NOW, None, FactState.CURRENT, "stage1", "model")["from"] is None


def test_with_event_appends_without_touching_its_input():
    first = lifecycle_event(NOW, None, FactState.CURRENT, "stage1", "model")
    second = lifecycle_event(NOW, FactState.CURRENT, FactState.ARCHIVED, "stage1", "model")
    events = [first]
    assert with_event(events, second) == [first, second]
    assert events == [first]


def test_the_event_list_keeps_the_newest_twenty():
    events = []
    for n in range(LIFECYCLE_EVENTS_CAP + 5):
        events = with_event(events, lifecycle_event(NOW, FactState.CURRENT, FactState.CURRENT, "stage1", f"e{n}"))
    assert LIFECYCLE_EVENTS_CAP == 20 and len(events) == 20
    assert [e["reason"] for e in events] == [f"e{n}" for n in range(5, 25)]


def test_with_event_copies_the_maps():
    stored = {"at": NOW, "from": None, "to": "current", "actor": "x", "reason": "r"}
    out = with_event([stored], lifecycle_event(NOW, None, FactState.CURRENT, "y", "r"))
    out[0]["reason"] = "changed"
    assert stored["reason"] == "r"


# --- the entity and the in-place set -----------------------------------------------------------------------------


def test_the_new_fields_default_to_no_decision():
    f = fact()
    assert f.expiry_kind is None and f.stale_since is None and f.lifecycle_events == []


def test_the_default_event_list_is_not_shared_between_facts():
    a, b = fact(), fact()
    a.lifecycle_events.append({"x": 1})
    assert b.lifecycle_events == []


def test_the_new_fields_round_trip_through_a_dump():
    f = fact(expiry_kind=ExpiryKind.HORIZON, ttl_days=14, stale_since=NOW, lifecycle_events=[{"to": "stale"}])
    again = FactEntity(**f.model_dump())
    assert again.expiry_kind is ExpiryKind.HORIZON and again.stale_since == NOW and again.lifecycle_events == [{"to": "stale"}]


def test_a_stored_kind_is_read_from_its_string():
    assert fact(expiry_kind="date").expiry_kind is ExpiryKind.DATE


def test_the_decision_fields_are_in_place_fields_and_content_is_not():
    assert {"expiry_kind", "ttl_days", "expires_at", "stale_since", "lifecycle_events"} <= IN_PLACE_FIELDS
    assert not IN_PLACE_FIELDS & {"text", "tags", "metadata", "temporal_class", "version", "lineage_id", "id"}
    assert set(closure_fields("next", NOW)) <= IN_PLACE_FIELDS


def test_historical_priority_is_read_as_archival():
    assert normalize_fact_taxonomy({"context_priority": "HISTORICAL"})["context_priority"] == "archival"
    assert normalize_fact_taxonomy({"context_priority": "historical"})["context_priority"] == ContextPriority.ARCHIVAL.value
    assert fact(**normalize_fact_taxonomy({"context_priority": "Historical"})).context_priority is ContextPriority.ARCHIVAL


def test_other_priorities_are_only_lowercased():
    assert normalize_fact_taxonomy({"context_priority": "HIGH", "state": "CURRENT"}) == {
        "context_priority": "high", "state": "current",
    }

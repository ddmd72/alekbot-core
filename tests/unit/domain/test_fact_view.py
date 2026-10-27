"""One view of a stored fact for the consolidator (Facts S4 T6c, M6), and the domain values in their own types (L4).

`fact_view` is what the Stage 2a cluster entry, the search hit and the review's fact lines are built on. The pinned values
below are the outputs of `_cluster_entry`, `consolidation_hit` and `_build_cluster_message` before T6c (commit 25f79c8) for
the same three facts: the view must equal them on every key they share, and its `reported` must be the day the cluster
message showed.
"""
import json
from dataclasses import asdict
from datetime import datetime, timezone

import pytest

from src.adapters.firestore_repo import _expiry_kind
from src.adapters.gcp_task_queue import _DomainEncoder
from src.agents.consolidation_agent import ConsolidationAgent, _session_time
from src.domain.entities import ExpiryKind, FactEntity, FactType
from src.domain.fact_expiry import expiry_view
from src.domain.fact_write_context import as_utc
from src.domain.search import EnrichedContext, EnrichedFact, consolidation_hit, fact_view

UTC = timezone.utc
LISBON = "Europe/Lisbon"


def stored(fact_id, text, fact_type, reported, **over):
    return FactEntity(
        id=fact_id, account_id="a", created_by_user_id="u", lineage_id=fact_id, text=text, type=fact_type,
        reported_date=reported, **over,
    )


FACTS = {
    # Reported and due both before the end of Lisbon's summer time (25 October): in winter Lisbon is UTC, and the
    # zone would not show in `due`.
    "horizon": stored(
        "f1", "takes the pills daily", FactType.EVENT, datetime(2026, 9, 22, 23, 30, tzinfo=UTC),
        expiry_kind=ExpiryKind.HORIZON, ttl_days=30, expires_at=datetime(2026, 10, 22, 23, 30, tzinfo=UTC),
    ),
    "date, naive reported": stored(
        "f2", "trip to Rome", FactType.STATE, datetime(2026, 10, 22, 10, 0),
        expiry_kind=ExpiryKind.DATE, expires_at=datetime(2027, 1, 15, 23, 0, tzinfo=UTC),
    ),
    "no decision": stored("f3", "legacy", FactType.STATE, datetime(2026, 3, 1, 12, 0, tzinfo=UTC)),
}

# The old outputs, in the owner's zone (Europe/Lisbon).
OLD_CLUSTER_ENTRY = {
    "horizon": {"fact_id": "f1", "content": "takes the pills daily", "similarity": 0.0,
                "expiry": {"kind": "horizon", "days": 30}, "due": "2026-10-23",
                "reported_date": "2026-09-23T00:30:00+01:00"},
    "date, naive reported": {"fact_id": "f2", "content": "trip to Rome", "similarity": 0.0,
                             "expiry": {"kind": "date", "date": "2027-01-15"}, "due": None,
                             "reported_date": "2026-10-22T11:00:00+01:00"},
    "no decision": {"fact_id": "f3", "content": "legacy", "similarity": 0.0, "expiry": None, "due": None,
                    "reported_date": "2026-03-01T12:00:00+00:00"},
}
OLD_SEARCH_HIT = {
    "horizon": {"fact_id": "f1", "content": "takes the pills daily", "similarity": 0.8, "source": "keyword",
                "type": "event", "domain": None, "temporal_class": None, "state": "current",
                "context_priority": "medium", "tags": [], "metadata": {},
                "reported_date": "2026-09-23T00:30:00+01:00", "expiry": {"kind": "horizon", "days": 30},
                "due": "2026-10-23", "version": 1},
    "date, naive reported": {"fact_id": "f2", "content": "trip to Rome", "similarity": 0.8, "source": "keyword",
                             "type": "state", "domain": None, "temporal_class": None, "state": "current",
                             "context_priority": "medium", "tags": [], "metadata": {},
                             "reported_date": "2026-10-22T11:00:00+01:00",
                             "expiry": {"kind": "date", "date": "2027-01-15"}, "due": None, "version": 1},
    "no decision": {"fact_id": "f3", "content": "legacy", "similarity": 0.8, "source": "keyword", "type": "state",
                    "domain": None, "temporal_class": None, "state": "current", "context_priority": "medium",
                    "tags": [], "metadata": {}, "reported_date": "2026-03-01T12:00:00+00:00", "expiry": None,
                    "due": None, "version": 1},
}
OLD_CLUSTER_LINE = {
    "horizon": '1. {"fact_id": "f1", "content": "takes the pills daily", "similarity": 0.0, '
               '"expiry": {"kind": "horizon", "days": 30}, "due": "2026-10-23", "reported": "2026-09-23"}',
    "date, naive reported": '1. {"fact_id": "f2", "content": "trip to Rome", "similarity": 0.0, '
                            '"expiry": {"kind": "date", "date": "2027-01-15"}, "reported": "2026-10-22"}',
    "no decision": '1. {"fact_id": "f3", "content": "legacy", "similarity": 0.0, "expiry": null, '
                   '"reported": "2026-03-01"}',
}
OLD_REPORTED_DAY = {"horizon": "2026-09-23", "date, naive reported": "2026-10-22", "no decision": "2026-03-01"}


def hit(fact):
    return EnrichedFact.from_fact(fact, "keyword", 0.8)


# ------------------------------------------------------------------------------------------------ one view

@pytest.mark.parametrize("name", FACTS)
def test_the_view_equals_the_old_cluster_entry_on_every_key_it_had(name):
    view = fact_view(FACTS[name], LISBON)
    old = {k: v for k, v in OLD_CLUSTER_ENTRY[name].items() if k != "similarity"}
    assert {k: view[k] for k in old} == old
    assert view["reported"] == OLD_REPORTED_DAY[name]


@pytest.mark.parametrize("name", FACTS)
def test_the_view_equals_the_old_search_hit_on_every_key_they_share(name):
    view = fact_view(FACTS[name], LISBON)
    shared = set(view) & set(OLD_SEARCH_HIT[name])
    assert shared == {"fact_id", "content", "expiry", "due", "reported_date"}
    assert {k: view[k] for k in shared} == {k: OLD_SEARCH_HIT[name][k] for k in shared}


@pytest.mark.parametrize("name", FACTS)
def test_the_document_and_its_search_hit_give_the_same_view(name):
    # Reviewer edit (T6c re-review, S3): `fact_view` takes the stored fact only; a hit's view is `consolidation_hit`,
    # built on the same core, so the hit carries the document's view on every view key.
    view, from_hit = fact_view(FACTS[name], LISBON), consolidation_hit(hit(FACTS[name]), LISBON)
    assert {k: from_hit[k] for k in view} == view


@pytest.mark.parametrize("name", FACTS)
def test_the_cluster_entry_is_the_old_entry_plus_the_reported_day(name):
    assert ConsolidationAgent._cluster_entry(FACTS[name], LISBON) == {
        **OLD_CLUSTER_ENTRY[name], "reported": OLD_REPORTED_DAY[name],
    }


@pytest.mark.parametrize("name", FACTS)
def test_the_search_hit_is_the_old_hit_plus_the_reported_day(name):
    assert consolidation_hit(hit(FACTS[name]), LISBON) == {**OLD_SEARCH_HIT[name], "reported": OLD_REPORTED_DAY[name]}


@pytest.mark.parametrize("name", FACTS)
def test_the_cluster_message_line_is_byte_identical_from_an_entry_and_from_a_search_hit(name):
    from_entry = ConsolidationAgent._build_cluster_message([ConsolidationAgent._cluster_entry(FACTS[name], LISBON)])
    from_hit = ConsolidationAgent._build_cluster_message([{**consolidation_hit(hit(FACTS[name]), LISBON), "similarity": 0.0}])
    assert from_entry.splitlines()[-1] == from_hit.splitlines()[-1] == OLD_CLUSTER_LINE[name]


def test_the_reported_day_is_the_owners_calendar_day_not_the_utc_day():
    fact = FACTS["horizon"]  # 23:30 UTC on 22 September is 00:30 on 23 September in Europe/Lisbon
    assert fact_view(fact, "UTC")["reported"] == "2026-09-22"
    assert fact_view(fact, LISBON)["reported"] == "2026-09-23"


def test_a_search_hit_without_a_reported_date_shows_none():
    view = consolidation_hit(EnrichedFact(fact_id="f", content="c", source="keyword"), LISBON)  # reviewer edit, S3
    assert (view["reported_date"], view["reported"], view["expiry"], view["due"]) == (None, None, None, None)


# ------------------------------------------------------------------------------------------------ values in their own types

def test_the_search_hit_holds_aware_datetimes_and_the_enum():
    got = hit(FACTS["date, naive reported"])
    assert got.reported_date == datetime(2026, 10, 22, 10, 0, tzinfo=UTC)
    assert got.expires_at == datetime(2027, 1, 15, 23, 0, tzinfo=UTC)
    assert got.reported_date.tzinfo is not None and got.expires_at.tzinfo is not None
    assert got.expiry_kind is ExpiryKind.DATE


def test_the_enriched_context_crosses_the_task_payload_encoder():
    context = EnrichedContext(facts=[hit(FACTS["horizon"])], total_sources=1, dedup_count=0, biographical_dedup_count=0)
    wire = json.loads(json.dumps({"enriched_context": asdict(context)}, cls=_DomainEncoder))
    (fact,) = wire["enriched_context"]["facts"]
    assert fact["reported_date"] == "2026-09-22T23:30:00+00:00"
    assert fact["expires_at"] == "2026-10-22T23:30:00+00:00"
    assert fact["expiry_kind"] == "horizon" and fact["ttl_days"] == 30


def test_the_task_payload_encoder_writes_dates_and_plain_enums():
    from src.domain.fact_review_outcome import ReviewDecision  # a dataclass that is no pydantic model
    from datetime import date
    from enum import Enum

    class Colour(Enum):
        RED = "red"

    wire = json.loads(json.dumps(
        {"on": date(2026, 10, 24), "colour": Colour.RED, "decision": asdict(ReviewDecision("f", "revived", "f", None))},
        cls=_DomainEncoder,
    ))
    assert wire == {"on": "2026-10-24", "colour": "red",
                    "decision": {"fact_id": "f", "outcome": "revived", "written_id": "f", "basis_kind": None,
                                 "as_history": False}}  # P2 revised (C8): ReviewDecision.as_history


def test_the_task_payload_encoder_still_refuses_what_it_does_not_know():
    with pytest.raises(TypeError):
        json.dumps({"x": object()}, cls=_DomainEncoder)


def test_expiry_view_takes_the_enum():
    midnight = datetime(2026, 7, 10, 23, 0, tzinfo=UTC)
    assert expiry_view(ExpiryKind.DATE, None, midnight, LISBON) == ({"kind": "date", "date": "2026-07-10"}, None)
    assert expiry_view(None, 7, midnight, LISBON) == (None, None)


@pytest.mark.parametrize("raw,kind", [
    ("date", ExpiryKind.DATE), ("horizon", ExpiryKind.HORIZON), ("none", ExpiryKind.NONE),
    ("forever", None), (None, None), (3, None), ([], None),
])
def test_the_repository_decodes_a_projected_kind(raw, kind):
    assert _expiry_kind(raw) is kind


def test_as_utc_takes_a_datetime_only():
    assert as_utc(datetime(2026, 10, 22, 12, 0)) == datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    assert as_utc(datetime(2026, 10, 22, 14, 0, tzinfo=timezone.utc).astimezone()) == datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
    for value in ("2026-10-22T12:00:00", 1792000000.0, None):
        with pytest.raises(TypeError):
            as_utc(value)  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------------ the session's times

@pytest.mark.parametrize("value", [1792000000.0, "2026-10-14T17:46:40Z", "2026-10-14T19:46:40+02:00", "2026-10-14T17:46:40",
                                   datetime(2026, 10, 14, 17, 46, 40)])
def test_a_session_time_is_read_as_the_same_utc_moment(value):
    assert _session_time(value) == datetime(2026, 10, 14, 17, 46, 40, tzinfo=UTC)


@pytest.mark.parametrize("value", ["INVALID", None])
def test_an_unreadable_session_time_raises(value):
    with pytest.raises((ValueError, TypeError)):
        _session_time(value)


def test_the_message_time_and_the_span_read_session_times():
    msgs = [{"created_at": 1792000000.0}, {"created_at": "", "timestamp": "2026-10-14T19:46:40+02:00"}, {"timestamp": "x"}]
    assert [ConsolidationAgent._message_time(m) for m in msgs] == [1792000000.0, "2026-10-14T19:46:40+02:00", "x"]
    moment = datetime(2026, 10, 14, 17, 46, 40, tzinfo=UTC)
    assert ConsolidationAgent._times_span([ConsolidationAgent._message_time(m) for m in msgs], "message times") == (moment, moment)

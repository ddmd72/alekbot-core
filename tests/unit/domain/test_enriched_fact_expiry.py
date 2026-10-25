"""`EnrichedFact` carries the stored expiry decision raw and reads it back to the model's view (Facts S3 · T3).

Reviewer edit (Facts S4 T6c, L4 and M6): `EnrichedFact` holds `expires_at` as an aware datetime and `expiry_kind` as the
enum, and the model's view is `consolidation_hit` (`expiry_decision` is gone). The cases are the same; a value that is not a
time or not a kind is now refused where it is decoded (the stored fact's validation, the repository's kind decoder), so
it never reaches the search hit.
"""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.domain.entities import ExpiryKind, FactEntity, FactType
from src.domain.search import EnrichedFact, consolidation_hit

UTC = timezone.utc


def fact(**over):
    return EnrichedFact(fact_id="f", content="c", source="s", **over)


def decision(f, zone):
    view = consolidation_hit(f, zone)  # T6c re-review, S3: a hit's view is consolidation_hit
    return view["expiry"], view["due"]


def test_a_fact_without_a_decision_has_no_view():
    f = fact()
    assert (f.expiry_kind, f.ttl_days, f.expires_at) == (None, None, None)
    assert decision(f, "Europe/Paris") == (None, None)


@pytest.mark.parametrize("zone,day", [("Europe/Paris", "2026-07-10"), ("UTC", "2026-07-10"), ("Pacific/Kiritimati", "2026-07-10")])
def test_a_date_decision_shows_the_last_local_day(zone, day):
    # Each instant is the start of 2026-07-11 in its zone, i.e. the end of the last day shown.
    instants = {"Europe/Paris": datetime(2026, 7, 10, 22, 0, tzinfo=UTC), "UTC": datetime(2026, 7, 11, 0, 0, tzinfo=UTC),
                "Pacific/Kiritimati": datetime(2026, 7, 10, 10, 0, tzinfo=UTC)}
    f = fact(expiry_kind=ExpiryKind.DATE, expires_at=instants[zone])
    assert decision(f, zone) == ({"kind": "date", "date": day}, None)


def test_a_horizon_decision_shows_days_and_the_local_due_date():
    f = fact(expiry_kind=ExpiryKind.HORIZON, ttl_days=14, expires_at=datetime(2026, 3, 1, 23, 30, tzinfo=UTC))
    assert decision(f, "Europe/Paris") == ({"kind": "horizon", "days": 14}, "2026-03-02")


def test_a_naive_time_is_read_as_utc():
    f = fact(expiry_kind=ExpiryKind.DATE, expires_at=datetime(2026, 7, 10, 22, 0))
    assert decision(f, "Europe/Paris") == ({"kind": "date", "date": "2026-07-10"}, None)


def test_a_stored_kind_string_becomes_the_member_before_the_hit_is_built():
    stored = FactEntity(id="f", account_id="a", created_by_user_id="u", lineage_id="f", text="c", type=FactType.STATE,
                        expiry_kind="date", expires_at=datetime(2026, 7, 10, 22, 0, tzinfo=UTC))  # type: ignore[arg-type]
    hit = EnrichedFact.from_fact(stored, "s", None)
    assert hit.expiry_kind is ExpiryKind.DATE
    assert decision(hit, "Europe/Paris")[0] == {"kind": "date", "date": "2026-07-10"}


def test_none_needs_no_date():
    assert decision(fact(expiry_kind=ExpiryKind.NONE), "UTC") == ({"kind": "none"}, None)


def test_a_malformed_expires_at_is_an_error_not_a_silent_null():
    with pytest.raises(ValidationError):
        FactEntity(id="f", account_id="a", created_by_user_id="u", lineage_id="f", text="c", type=FactType.STATE,
                   expiry_kind=ExpiryKind.DATE, expires_at="soon")  # type: ignore[arg-type]

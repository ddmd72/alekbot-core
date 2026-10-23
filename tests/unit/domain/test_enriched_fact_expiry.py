"""`EnrichedFact` carries the stored expiry decision raw and reads it back to the model's view (Facts S3 · T3)."""
import pytest

from src.domain.entities import ExpiryKind
from src.domain.search import EnrichedFact


def fact(**over):
    return EnrichedFact(fact_id="f", content="c", source="s", **over)


def test_a_fact_without_a_decision_has_no_view():
    f = fact()
    assert (f.expiry_kind, f.ttl_days, f.expires_at) == (None, None, None)
    assert f.expiry_decision("Europe/Paris") == (None, None)


@pytest.mark.parametrize("zone,day", [("Europe/Paris", "2026-07-10"), ("UTC", "2026-07-10"), ("Pacific/Kiritimati", "2026-07-10")])
def test_a_date_decision_shows_the_last_local_day(zone, day):
    # Each instant is the start of 2026-07-11 in its zone, i.e. the end of the last day shown.
    instants = {"Europe/Paris": "2026-07-10T22:00:00+00:00", "UTC": "2026-07-11T00:00:00+00:00",
                "Pacific/Kiritimati": "2026-07-10T10:00:00+00:00"}
    f = fact(expiry_kind=ExpiryKind.DATE, expires_at=instants[zone])
    assert f.expiry_decision(zone) == ({"kind": "date", "date": day}, None)


def test_a_horizon_decision_shows_days_and_the_local_due_date():
    f = fact(expiry_kind=ExpiryKind.HORIZON, ttl_days=14, expires_at="2026-03-01T23:30:00+00:00")
    assert f.expiry_decision("Europe/Paris") == ({"kind": "horizon", "days": 14}, "2026-03-02")


def test_a_naive_iso_string_is_read_as_utc():
    f = fact(expiry_kind=ExpiryKind.DATE, expires_at="2026-07-10T22:00:00")
    assert f.expiry_decision("Europe/Paris") == ({"kind": "date", "date": "2026-07-10"}, None)


def test_a_stored_kind_string_is_understood_like_the_member():
    f = fact(expiry_kind="date", expires_at="2026-07-10T22:00:00+00:00")  # type: ignore[arg-type]
    assert f.expiry_decision("Europe/Paris")[0] == {"kind": "date", "date": "2026-07-10"}


def test_none_needs_no_date():
    assert fact(expiry_kind=ExpiryKind.NONE).expiry_decision("UTC") == ({"kind": "none"}, None)


def test_a_malformed_expires_at_is_an_error_not_a_silent_null():
    with pytest.raises(ValueError):
        fact(expiry_kind=ExpiryKind.DATE, expires_at="soon").expiry_decision("UTC")

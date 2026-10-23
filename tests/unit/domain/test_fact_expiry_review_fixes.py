"""T1 review fixes: string kinds from a projection, overflow at parse, a naive now, an Expiry without its data, minimality of N."""

from datetime import datetime, timedelta, timezone

import pytest

from src.domain.entities import ExpiryKind, FactDomain, FactType
from src.domain.exceptions import FactExpiryError
from src.domain.fact_expiry import NOTHING_WRITTEN, Expiry, as_expiry_kind, expiry_view, parse_expiry
from src.domain.fact_lifecycle import born_due, compute_expiry, expiry_pinned
from tests.support.fact_write import horizon, on

PARIS = "Europe/Paris"
NOW = datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


# --- F1: a kind that arrives as a string -------------------------------------------------------------------------


def test_as_expiry_kind_accepts_members_and_known_strings_only():
    assert as_expiry_kind(ExpiryKind.DATE) is ExpiryKind.DATE
    assert as_expiry_kind("horizon") is ExpiryKind.HORIZON
    assert as_expiry_kind("forever") is None and as_expiry_kind(None) is None and as_expiry_kind(3) is None


def test_expiry_view_reads_string_kinds_like_members():
    midnight = utc(2026, 7, 10, 22, 0)
    assert expiry_view("none", None, None, PARIS) == ({"kind": "none"}, None)
    assert expiry_view("date", None, midnight, PARIS) == ({"kind": "date", "date": "2026-07-10"}, None)
    assert expiry_view("horizon", 7, midnight, PARIS) == ({"kind": "horizon", "days": 7}, "2026-07-11")
    assert expiry_view("nonsense", 7, midnight, PARIS) == (None, None)


def string_fields(expiry, reported):
    got = compute_expiry(parse_expiry(expiry), reported, PARIS)
    return {**got, "expiry_kind": got["expiry_kind"].value}


@pytest.mark.parametrize("cleanup", [False, True])
def test_a_past_date_given_as_a_string_kind_is_refused_on_every_pass(cleanup):
    reported = NOW - timedelta(hours=1)
    with pytest.raises(FactExpiryError, match="is past"):
        born_due(string_fields(on("2026-10-20"), reported), reported, NOW, PARIS, cleanup)


def test_a_horizon_given_as_a_string_kind_is_floored_on_a_cleanup_pass_and_refused_otherwise():
    reported = NOW - timedelta(days=30)
    due = string_fields(horizon(14), reported)
    floored, note = born_due(due, reported, NOW, PARIS, True)
    assert floored["expires_at"] == NOW + timedelta(days=2) and floored["expiry_kind"] == "horizon" and note
    with pytest.raises(FactExpiryError, match="horizon of 14 days"):
        born_due(due, reported, NOW, PARIS, False)


def test_a_string_none_kind_is_never_due():
    fields = {"expiry_kind": "none", "ttl_days": None, "expires_at": None}
    assert born_due(fields, NOW, NOW, PARIS, False) == (fields, None)


def test_pinning_reads_domain_and_type_given_as_strings():
    assert expiry_pinned("agent_directive", FactType.EVENT, []) is True
    assert expiry_pinned(None, "principle", []) is True
    assert expiry_pinned("work", "event", []) is False
    assert expiry_pinned(FactDomain.AGENT_DIRECTIVE, FactType.EVENT, []) is True


# --- F2: overflow is a tool error --------------------------------------------------------------------------------


@pytest.mark.parametrize("days", [36501, 10**7, 10**30])
def test_an_enormous_horizon_is_a_fact_expiry_error(days):
    with pytest.raises(FactExpiryError) as refused:
        parse_expiry(horizon(days))
    assert "36500" in str(refused.value) and NOTHING_WRITTEN in str(refused.value)


def test_the_largest_horizon_computes():
    got = compute_expiry(parse_expiry(horizon(36500)), NOW, PARIS)
    assert got["expires_at"] == NOW + timedelta(days=36500)


@pytest.mark.parametrize("day", ["9999-12-31", "9999-01-01", "0001-01-01", "1899-12-31"])
def test_a_date_outside_the_calendar_we_compute_is_a_fact_expiry_error(day):
    with pytest.raises(FactExpiryError) as refused:
        parse_expiry(on(day))
    assert "between 1900 and 9998" in str(refused.value) and NOTHING_WRITTEN in str(refused.value)


@pytest.mark.parametrize("day", ["1900-01-01", "9998-12-31"])
@pytest.mark.parametrize("zone", ["Pacific/Kiritimati", "Pacific/Pago_Pago", "UTC", PARIS])
def test_the_edge_dates_compute_in_every_zone_without_overflow(day, zone):
    assert compute_expiry(parse_expiry(on(day)), NOW, zone)["expires_at"].year in (1900, 1901, 9998, 9999)


# --- F3: a naive now is UTC --------------------------------------------------------------------------------------


def test_born_due_reads_a_naive_now_as_utc():
    reported = NOW - timedelta(days=30)
    fields = compute_expiry(parse_expiry(horizon(14)), reported, PARIS)
    naive = NOW.replace(tzinfo=None)
    assert born_due(fields, reported, naive, PARIS, True) == born_due(fields, reported, NOW, PARIS, True)
    with pytest.raises(FactExpiryError) as aware_refusal:
        born_due(fields, reported, NOW, PARIS, False)
    with pytest.raises(FactExpiryError) as naive_refusal:
        born_due(fields, reported, naive, PARIS, False)
    assert str(naive_refusal.value) == str(aware_refusal.value)
    with pytest.raises(FactExpiryError, match="is past"):
        born_due(compute_expiry(parse_expiry(on("2026-10-20")), reported, PARIS), reported, naive, PARIS, False)


# --- F4 ----------------------------------------------------------------------------------------------------------


def test_an_expiry_cannot_be_built_without_its_data():
    with pytest.raises(ValueError, match="date"):
        Expiry(ExpiryKind.DATE)
    with pytest.raises(ValueError, match="horizon"):
        Expiry(ExpiryKind.HORIZON)
    assert Expiry(ExpiryKind.NONE).kind is ExpiryKind.NONE


def test_an_expiry_built_with_a_string_kind_is_the_member():
    assert Expiry("none").kind is ExpiryKind.NONE


def test_a_date_after_today_refused_on_a_23_hour_dst_day_does_not_say_today():
    now = utc(2026, 3, 28, 22, 30)  # 23:30 on the 28th in Paris; the 29th is only 23 hours long
    fields = compute_expiry(parse_expiry(on("2026-03-29")), now, PARIS)
    with pytest.raises(FactExpiryError) as refused:
        born_due(fields, now, now, PARIS, False)
    assert "a plan ending 2026-03-29 is too close" in str(refused.value) and "today" not in str(refused.value)


@pytest.mark.parametrize("age_days", [0, 0.4, 3, 11, 29.5, 400])
def test_the_minimum_in_the_message_is_the_smallest_that_passes_a_day_later(age_days):
    reported = NOW - timedelta(days=age_days)
    with pytest.raises(FactExpiryError) as refused:
        born_due(compute_expiry(parse_expiry(horizon(1)), reported, PARIS), reported, NOW, PARIS, False)
    given = int(str(refused.value).split("give at least ")[1].split(" days")[0])
    a_day_later = NOW + timedelta(days=1)
    born_due(compute_expiry(parse_expiry(horizon(given)), reported, PARIS), reported, a_day_later, PARIS, False)
    if given > 1:
        with pytest.raises(FactExpiryError):
            born_due(compute_expiry(parse_expiry(horizon(given - 1)), reported, PARIS), reported, a_day_later, PARIS, False)

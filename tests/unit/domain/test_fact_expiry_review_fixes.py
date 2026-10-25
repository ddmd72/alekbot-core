"""T1 review fixes: string kinds from a projection, overflow at parse, a naive now, an Expiry without its data, minimality of N."""

from datetime import datetime, timedelta, timezone

import pytest

from src.domain.entities import ExpiryKind, FactDomain, FactType
from src.domain.exceptions import FactExpiryError
from src.adapters.firestore_repo import _expiry_kind
from src.domain.fact_expiry import NOTHING_WRITTEN, Expiry, expiry_view, parse_expiry
from src.domain.fact_lifecycle import born_due, compute_expiry, expiry_pinned
from src.domain.pass_actor import PassActor
from tests.support.fact_write import horizon, on

PARIS = "Europe/Paris"
NOW = datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc)
CONVERSATION = PassActor.STAGE1  # no floor, the Stage 1 wording
CLEANUP = PassActor.STAGE2A  # floors a due horizon, the cleanup wording


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


# --- F1: a kind that arrives as a string -------------------------------------------------------------------------


# Reviewer edit (Facts S4 T6c, L4): a stored kind string is decoded by the repository (`_expiry_kind`, beside the raw
# projection), no longer by the domain's `as_expiry_kind`; the domain's `expiry_view` takes the enum only. Same cases.
def test_the_repository_decoder_accepts_members_and_known_strings_only():
    assert _expiry_kind(ExpiryKind.DATE) is ExpiryKind.DATE
    assert _expiry_kind("horizon") is ExpiryKind.HORIZON
    assert _expiry_kind("forever") is None and _expiry_kind(None) is None and _expiry_kind(3) is None


def test_expiry_view_reads_each_member_and_no_kind_as_no_view():
    midnight = utc(2026, 7, 10, 22, 0)
    assert expiry_view(ExpiryKind.NONE, None, None, PARIS) == ({"kind": "none"}, None)
    assert expiry_view(ExpiryKind.DATE, None, midnight, PARIS) == ({"kind": "date", "date": "2026-07-10"}, None)
    assert expiry_view(ExpiryKind.HORIZON, 7, midnight, PARIS) == ({"kind": "horizon", "days": 7}, "2026-07-11")
    assert expiry_view(_expiry_kind("nonsense"), 7, midnight, PARIS) == (None, None)


# Reviewer edit (Facts S4 T6c, L4): `born_due` reads `compute_expiry`'s fields, whose kind is the member; a stored kind
# string is decoded by the repository before any domain rule sees it. The three F1 cases keep their assertions with the
# member (the string form is no longer a domain input).
def decided_fields(expiry, reported):
    got = compute_expiry(parse_expiry(expiry), reported, PARIS)
    assert isinstance(got["expiry_kind"], ExpiryKind)
    return got


@pytest.mark.parametrize("cleanup", [False, True])
def test_a_past_date_is_refused_on_every_pass(cleanup):
    reported = NOW - timedelta(hours=1)
    with pytest.raises(FactExpiryError, match="is past"):
        born_due(decided_fields(on("2026-10-20"), reported), reported, NOW, PARIS, CLEANUP if cleanup else CONVERSATION)


def test_a_due_horizon_is_floored_on_a_cleanup_pass_and_refused_otherwise():
    reported = NOW - timedelta(days=30)
    due = decided_fields(horizon(14), reported)
    floored, note = born_due(due, reported, NOW, PARIS, CLEANUP)
    assert floored["expires_at"] == NOW + timedelta(days=2) and floored["expiry_kind"] is ExpiryKind.HORIZON and note
    with pytest.raises(FactExpiryError, match="horizon of 14 days"):
        born_due(due, reported, NOW, PARIS, CONVERSATION)


def test_a_none_kind_is_never_due():
    fields = {"expiry_kind": ExpiryKind.NONE, "ttl_days": None, "expires_at": None}
    assert born_due(fields, NOW, NOW, PARIS, CONVERSATION) == (fields, None)


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
    assert born_due(fields, reported, naive, PARIS, CLEANUP) == born_due(fields, reported, NOW, PARIS, CLEANUP)
    with pytest.raises(FactExpiryError) as aware_refusal:
        born_due(fields, reported, NOW, PARIS, CONVERSATION)
    with pytest.raises(FactExpiryError) as naive_refusal:
        born_due(fields, reported, naive, PARIS, CONVERSATION)
    assert str(naive_refusal.value) == str(aware_refusal.value)
    with pytest.raises(FactExpiryError, match="is past"):
        born_due(compute_expiry(parse_expiry(on("2026-10-20")), reported, PARIS), reported, naive, PARIS, CONVERSATION)


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
    now = utc(2026, 3, 28, 22, 30)  # 23:30 on the 28th in Europe/Paris; the 29th is only 23 hours long
    fields = compute_expiry(parse_expiry(on("2026-03-29")), now, PARIS)
    with pytest.raises(FactExpiryError) as refused:
        born_due(fields, now, now, PARIS, CONVERSATION)
    assert "a plan ending 2026-03-29 is too close" in str(refused.value) and "today" not in str(refused.value)


@pytest.mark.parametrize("age_days", [0, 0.4, 3, 11, 29.5, 400])
def test_the_minimum_in_the_message_is_the_smallest_that_passes_a_day_later(age_days):
    reported = NOW - timedelta(days=age_days)
    with pytest.raises(FactExpiryError) as refused:
        born_due(compute_expiry(parse_expiry(horizon(1)), reported, PARIS), reported, NOW, PARIS, CONVERSATION)
    given = int(str(refused.value).split("give at least ")[1].split(" days")[0])
    a_day_later = NOW + timedelta(days=1)
    born_due(compute_expiry(parse_expiry(horizon(given)), reported, PARIS), reported, a_day_later, PARIS, CONVERSATION)
    if given > 1:
        with pytest.raises(FactExpiryError):
            born_due(compute_expiry(parse_expiry(horizon(given - 1)), reported, PARIS), reported, a_day_later, PARIS, CONVERSATION)

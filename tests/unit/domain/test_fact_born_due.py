"""born_due: no expiry is born due; a horizon on a cleanup pass is floored instead (Facts S3, plan §3.6)."""

from datetime import datetime, timedelta, timezone

import pytest

from src.domain.entities import ExpiryKind
from src.domain.exceptions import FactExpiryError
from src.domain.fact_expiry import NOTHING_WRITTEN, parse_expiry
from src.domain.fact_lifecycle import born_due, compute_expiry
from tests.support.fact_write import EXPIRY_NONE, horizon, on

PARIS = "Europe/Paris"
NOW = datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc)  # 11:00 in Paris
REPORTED = NOW - timedelta(hours=2)


def fields(expiry, reported=REPORTED):
    return compute_expiry(parse_expiry(expiry), reported, PARIS)


def check(expiry, *, cleanup=False, reported=REPORTED, now=NOW):
    return born_due(fields(expiry, reported), reported, now, PARIS, cleanup)


# --- kind date: refused on both pass kinds -------------------------------------------------------------------------


@pytest.mark.parametrize("cleanup", [False, True])
def test_a_past_date_is_refused_with_the_discard_or_history_text(cleanup):
    with pytest.raises(FactExpiryError) as refused:
        check(on("2026-10-20"), cleanup=cleanup)
    text = str(refused.value)
    assert "the date 2026-10-20 is past (today is 2026-10-23 in Europe/Paris)" in text
    assert "{kind: none}" in text and "past tense" in text
    # S3-D12: only a conversation pass may discard its candidate; a cleanup pass reviews stored memory.
    assert ("DISCARD the candidate" in text) is (not cleanup)
    assert text.endswith(NOTHING_WRITTEN)


@pytest.mark.parametrize("cleanup", [False, True])
def test_a_date_of_today_asks_for_a_short_horizon(cleanup):
    with pytest.raises(FactExpiryError) as refused:
        check(on("2026-10-23"), cleanup=cleanup)
    text = str(refused.value)
    assert "a plan for today is not over yet" in text and "short horizon of at least 3 days" in text
    assert "past" not in text and text.endswith(NOTHING_WRITTEN)


def test_yesterday_in_the_owners_zone_is_past_even_when_it_is_still_today_in_utc():
    late_night = datetime(2026, 10, 23, 22, 30, tzinfo=timezone.utc)  # 00:30 on the 24th in Paris
    with pytest.raises(FactExpiryError, match="is past"):
        check(on("2026-10-23"), reported=late_night - timedelta(hours=1), now=late_night)


def test_tomorrow_passes_untouched():
    got, note = check(on("2026-10-24"))
    assert got == fields(on("2026-10-24")) and note is None


# --- kind horizon, Stage 1 and email: refused ----------------------------------------------------------------------


def test_the_edge_now_plus_one_day_is_refused_and_one_second_later_is_accepted():
    reported = NOW - timedelta(days=9)
    edge = (NOW + timedelta(days=1) - reported).days  # 10 days from reported lands exactly on now + 1 day
    with pytest.raises(FactExpiryError):
        born_due(compute_expiry(parse_expiry(horizon(edge)), reported, PARIS), reported, NOW, PARIS, False)
    just_after = compute_expiry(parse_expiry(horizon(edge)), reported + timedelta(seconds=1), PARIS)
    assert born_due(just_after, reported, NOW, PARIS, False) == (just_after, None)


def test_a_due_horizon_is_refused_with_the_date_the_minimum_and_the_ways_out():
    reported = NOW - timedelta(days=30)
    with pytest.raises(FactExpiryError) as refused:
        born_due(fields(horizon(14), reported), reported, NOW, PARIS, False)
    text = str(refused.value)
    assert "counted from when the owner said it (2026-09-23), a horizon of 14 days ended 2026-10-07" in text
    assert "give at least 33 days" in text and "{kind: none}" in text and "DISCARD it" in text
    assert text.endswith(NOTHING_WRITTEN)


def test_a_horizon_ending_soon_but_not_yet_says_ends_not_ended():
    reported = NOW - timedelta(days=13)
    with pytest.raises(FactExpiryError, match="14 days ends 2026-10-24"):
        born_due(fields(horizon(14), reported), reported, NOW, PARIS, False)


@pytest.mark.parametrize("age_days", [0, 0.4, 3, 11, 29.5, 400])
def test_the_minimum_in_the_message_still_passes_ten_minutes_later(age_days):
    reported = NOW - timedelta(days=age_days)
    with pytest.raises(FactExpiryError) as refused:
        born_due(fields(horizon(1), reported), reported, NOW, PARIS, False)
    given = int(str(refused.value).split("give at least ")[1].split(" days")[0])
    later = NOW + timedelta(minutes=10)
    got, note = born_due(fields(horizon(given), reported), reported, later, PARIS, False)
    assert note is None and got["ttl_days"] == given


def test_a_fresh_horizon_passes_untouched():
    got, note = check(horizon(14))
    assert got == fields(horizon(14)) and note is None


# --- kind horizon, cleanup pass: floored ---------------------------------------------------------------------------


def test_a_due_horizon_on_a_cleanup_pass_is_floored_to_now_plus_two_days_and_keeps_kind_and_days():
    reported = NOW - timedelta(days=30)
    got, note = born_due(fields(horizon(14), reported), reported, NOW, PARIS, True)
    assert got["expires_at"] == NOW + timedelta(days=2)
    assert got["expiry_kind"] is ExpiryKind.HORIZON and got["ttl_days"] == 14
    assert note == (
        "counted from 2026-09-23 this horizon had already passed; the fact goes to review on 2026-10-25"
    )


def test_the_floor_applies_at_the_edge_and_not_after_it():
    reported = NOW - timedelta(days=9)
    at_edge = compute_expiry(parse_expiry(horizon(10)), reported, PARIS)
    assert born_due(at_edge, reported, NOW, PARIS, True)[0]["expires_at"] == NOW + timedelta(days=2)
    after_edge = compute_expiry(parse_expiry(horizon(10)), reported + timedelta(seconds=1), PARIS)
    assert born_due(after_edge, reported, NOW, PARIS, True) == (after_edge, None)


def test_the_floor_does_not_mutate_its_input():
    reported = NOW - timedelta(days=30)
    original = fields(horizon(14), reported)
    snapshot = dict(original)
    born_due(original, reported, NOW, PARIS, True)
    assert original == snapshot


# --- none -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("cleanup", [False, True])
def test_none_is_never_due(cleanup):
    got, note = check(EXPIRY_NONE, cleanup=cleanup, reported=NOW - timedelta(days=900))
    assert got == fields(EXPIRY_NONE) and note is None


# --- every refusal ends with the closing sentence ------------------------------------------------------------------


@pytest.mark.parametrize(
    "expiry, reported_days_ago",
    [(on("2026-10-01"), 1), (on("2026-10-23"), 1), (horizon(3), 30)],
)
def test_every_refusal_ends_with_nothing_was_written(expiry, reported_days_ago):
    reported = NOW - timedelta(days=reported_days_ago)
    with pytest.raises(FactExpiryError) as refused:
        born_due(fields(expiry, reported), reported, NOW, PARIS, False)
    assert str(refused.value).endswith("Nothing was written. Resend this call with a valid expiry before retiring any fact it replaces.")

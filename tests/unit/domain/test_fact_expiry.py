"""parse_expiry, compute_expiry and expiry_view: the expiry decision the model sends and sees (Facts S3, plan §3.3)."""

from datetime import date, datetime, timedelta, timezone

import pytest

from src.domain.entities import ExpiryKind
from src.domain.exceptions import FactExpiryError
from src.domain.fact_expiry import EXPIRY_NONE, NOTHING_WRITTEN, Expiry, expiry_view, parse_expiry
from src.domain.fact_lifecycle import compute_expiry
from tests.support.fact_write import horizon, on
from tests.support.fact_write import EXPIRY_NONE as NONE_LITERAL

LISBON = "Europe/Lisbon"
REPORTED = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


# --- parse_expiry -------------------------------------------------------------------------------------------------


def test_the_three_shapes_parse():
    assert parse_expiry(on("2026-09-25")) == Expiry(ExpiryKind.DATE, on=date(2026, 9, 25))
    assert parse_expiry(horizon(14)) == Expiry(ExpiryKind.HORIZON, days=14)
    assert parse_expiry(NONE_LITERAL) is EXPIRY_NONE


@pytest.mark.parametrize("raw", ["none", "None", " NONE "])
def test_the_string_none_is_the_whole_object(raw):
    assert parse_expiry(raw) is EXPIRY_NONE


def test_kind_is_case_insensitive():
    assert parse_expiry({"kind": "HORIZON", "days": 3}) == Expiry(ExpiryKind.HORIZON, days=3)
    assert parse_expiry({"kind": " Date ", "date": "2026-09-25"}).on == date(2026, 9, 25)


def test_keys_that_do_not_belong_to_the_kind_are_ignored():
    assert parse_expiry({"kind": "none", "days": 3, "date": "x"}) is EXPIRY_NONE
    assert parse_expiry({"kind": "horizon", "days": 3, "date": "garbage"}) == Expiry(ExpiryKind.HORIZON, days=3)
    assert parse_expiry({"kind": "date", "date": "2026-09-25", "days": -1}).days is None


@pytest.mark.parametrize(
    "raw",
    ["2026-09-25", "2026-09-25T10:30:00Z", "2026-09-25T23:59:00+02:00", datetime(2026, 9, 25, 23, 0), date(2026, 9, 25)],
)
def test_a_date_accepts_a_day_a_datetime_or_its_string(raw):
    assert parse_expiry({"kind": "date", "date": raw}).on == date(2026, 9, 25)


def test_days_accepts_a_whole_number_sent_as_a_float():
    assert parse_expiry({"kind": "horizon", "days": 14.0}).days == 14


# Reviewer edit (facts S3, T5 review F7; repo rule "accept liberally" for model-facing tool input): a digit string such
# as "14" is now accepted like the whole-number float 14.0 (pinned in test_fact_expiry_digit_days.py), so it leaves the
# refused list; "14 days" keeps a non-numeric string in it. A delta to plan §3.3 ("an int >= 1"), for "S3 as built".
@pytest.mark.parametrize("days", [0, -3, True, False, 2.5, "14 days", None, [14]])
def test_a_bad_days_is_refused(days):
    with pytest.raises(FactExpiryError, match="days"):
        parse_expiry({"kind": "horizon", "days": days})


@pytest.mark.parametrize("day", [None, "", "tomorrow", "2026-13-40", 20260925, "25.09.2026"])
def test_a_bad_date_is_refused(day):
    with pytest.raises(FactExpiryError, match="YYYY-MM-DD"):
        parse_expiry({"kind": "date", "date": day})


@pytest.mark.parametrize("raw", [{}, {"kind": "forever"}, {"kind": None}, {"kind": 3}, {"days": 3}, "soon", 7, [], True])
def test_a_malformed_expiry_names_the_three_shapes_and_that_nothing_was_written(raw):
    with pytest.raises(FactExpiryError) as refused:
        parse_expiry(raw)
    text = str(refused.value)
    assert "{kind: date, date: YYYY-MM-DD}" in text and "{kind: horizon, days: N}" in text and "{kind: none}" in text
    assert NOTHING_WRITTEN in text


def test_null_is_not_an_answer_and_the_text_says_what_to_send_instead():
    with pytest.raises(FactExpiryError) as refused:
        parse_expiry(None)
    assert "`null` is not an answer" in str(refused.value) and "{kind: none}" in str(refused.value)
    assert NOTHING_WRITTEN in str(refused.value)


def test_a_fact_expiry_error_is_a_value_error_so_tool_errors_already_map_it():
    assert issubclass(FactExpiryError, ValueError)


# --- compute_expiry -----------------------------------------------------------------------------------------------


def test_a_date_is_due_at_the_end_of_that_day_in_the_owners_zone_summer():
    got = compute_expiry(parse_expiry(on("2026-07-10")), REPORTED, LISBON)
    assert got == {"expiry_kind": ExpiryKind.DATE, "ttl_days": None, "expires_at": utc(2026, 7, 10, 23, 0)}


def test_a_date_is_due_at_the_end_of_that_day_in_the_owners_zone_winter():
    got = compute_expiry(parse_expiry(on("2026-01-10")), REPORTED, LISBON)
    assert got["expires_at"] == utc(2026, 1, 11, 0, 0)


def test_a_date_across_the_dst_nights_is_the_next_local_midnight():
    spring_day = compute_expiry(parse_expiry(on("2026-03-29")), REPORTED, LISBON)["expires_at"]  # a 23 h day
    day_before = compute_expiry(parse_expiry(on("2026-03-28")), REPORTED, LISBON)["expires_at"]
    autumn_day = compute_expiry(parse_expiry(on("2026-10-25")), REPORTED, LISBON)["expires_at"]  # a 25 h day
    assert day_before == utc(2026, 3, 29, 0, 0)
    assert spring_day == utc(2026, 3, 29, 23, 0)
    assert autumn_day == utc(2026, 10, 26, 0, 0)


def test_the_zone_decides_which_instant_ends_the_day():
    assert compute_expiry(parse_expiry(on("2026-07-10")), REPORTED, "UTC")["expires_at"] == utc(2026, 7, 11, 0, 0)
    assert compute_expiry(parse_expiry(on("2026-07-10")), REPORTED, "Pacific/Auckland")["expires_at"] == utc(2026, 7, 10, 12, 0)


def test_a_horizon_counts_days_from_the_reported_date():
    got = compute_expiry(parse_expiry(horizon(14)), REPORTED, LISBON)
    assert got == {"expiry_kind": ExpiryKind.HORIZON, "ttl_days": 14, "expires_at": REPORTED + timedelta(days=14)}


def test_a_horizon_from_a_naive_reported_date_reads_it_as_utc():
    got = compute_expiry(parse_expiry(horizon(1)), datetime(2026, 10, 22, 10, 0), LISBON)
    assert got["expires_at"] == utc(2026, 10, 23, 10, 0)


def test_none_stores_nulls():
    assert compute_expiry(EXPIRY_NONE, REPORTED, LISBON) == {"expiry_kind": ExpiryKind.NONE, "ttl_days": None, "expires_at": None}


# --- expiry_view --------------------------------------------------------------------------------------------------


def test_none_is_shown_as_none_without_due():
    assert expiry_view(ExpiryKind.NONE, None, None, LISBON) == ({"kind": "none"}, None)


def test_a_horizon_is_shown_with_its_days_and_a_read_only_due_date():
    stored = compute_expiry(parse_expiry(horizon(14)), REPORTED, LISBON)
    view, due = expiry_view(stored["expiry_kind"], stored["ttl_days"], stored["expires_at"], LISBON)
    assert view == {"kind": "horizon", "days": 14} and due == "2026-11-05"


def test_due_is_the_local_date_not_the_utc_date():
    expires = utc(2026, 7, 5, 23, 30)  # 00:30 on the 6th in Europe/Lisbon
    assert expiry_view(ExpiryKind.HORIZON, 5, expires, LISBON)[1] == "2026-07-06"


@pytest.mark.parametrize("day", ["2026-07-10", "2026-01-10", "2026-03-29", "2026-10-25", "2026-12-31"])
def test_a_date_fact_rendered_and_parsed_back_gives_the_same_expires_at(day):
    stored = compute_expiry(parse_expiry(on(day)), REPORTED, LISBON)
    view, due = expiry_view(stored["expiry_kind"], stored["ttl_days"], stored["expires_at"], LISBON)
    assert view == {"kind": "date", "date": day} and due is None
    assert compute_expiry(parse_expiry(view), REPORTED, LISBON) == stored


def test_a_horizon_view_sent_back_keeps_its_expires_at():
    stored = compute_expiry(parse_expiry(horizon(30)), REPORTED, LISBON)
    view, _ = expiry_view(stored["expiry_kind"], stored["ttl_days"], stored["expires_at"], LISBON)
    assert compute_expiry(parse_expiry(view), REPORTED, LISBON) == stored


def test_no_kind_is_no_view_and_ttl_days_alone_is_never_shown():
    assert expiry_view(None, 90, utc(2027, 1, 1), LISBON) == (None, None)
    assert expiry_view(None, None, None, LISBON) == (None, None)


def test_due_only_for_horizons():
    assert expiry_view(ExpiryKind.DATE, None, utc(2026, 7, 10, 23, 0), LISBON)[1] is None
    assert expiry_view(ExpiryKind.NONE, None, None, LISBON)[1] is None
    assert expiry_view(ExpiryKind.HORIZON, 7, utc(2026, 7, 10, 23, 0), LISBON)[1] == "2026-07-11"


def test_a_kind_without_its_data_is_not_shown_as_a_decision_the_model_could_not_resend():
    assert expiry_view(ExpiryKind.DATE, None, None, LISBON) == (None, None)
    assert expiry_view(ExpiryKind.HORIZON, None, utc(2026, 7, 10), LISBON) == (None, None)


def test_a_naive_expires_at_is_read_as_utc_and_an_unknown_zone_as_utc():
    assert expiry_view(ExpiryKind.HORIZON, 5, datetime(2026, 7, 5, 23, 30), LISBON)[1] == "2026-07-06"
    assert expiry_view(ExpiryKind.HORIZON, 5, utc(2026, 7, 5, 23, 30), "Not/AZone")[1] == "2026-07-05"

"""Dates printed without a year (fact profile RFC §4.4): the model writes `--MM-DD` / `--MM`, code fills the year the
fact was recorded. Invented cast only."""
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.drive_grade import TextEvidence
from src.domain.fact_profile_answer import AnswerTimePoint
from src.domain.fact_profile_derive import check_time_points, derive_profile, recording_year
from src.domain.place_names import PlaceNames

YEAR = 2026


def point(start, end, precision, source):
    return AnswerTimePoint.model_validate({"from": start, "to": end, "precision": precision, "source": source})


def check(text, points, year=YEAR):
    drops = {}
    evidence = TextEvidence([text])
    kept = check_time_points(points, evidence, text.casefold(), [], drops, year)
    return [(p.start, p.end, p.precision) for p in kept], drops


def test_a_day_printed_without_a_year_takes_the_recording_year():
    kept, drops = check("Hamburg conference Oct 15", [point("--10-15", "", "day", "Oct 15")])
    assert kept == [("2026-10-15", "", "day")] and drops == {}


def test_a_range_printed_without_a_year_takes_the_recording_year_at_both_ends():
    kept, _ = check("weekend in Coimbra with Clara Mar 6–8", [point("--03-06", "--03-08", "day", "Mar 6–8")])
    assert kept == [("2026-03-06", "2026-03-08", "day")]


def test_a_month_printed_without_a_year_takes_the_recording_year():
    kept, _ = check("Hamburg conference in October", [point("--10", "", "month", "October")])
    assert kept == [("2026-10", "", "month")]


def test_a_span_over_the_new_year_puts_its_end_in_the_next_year():
    kept, _ = check("Payment window Dec 25 to Jan 1", [point("--12-25", "--01-01", "day", "Dec 25 to Jan 1")])
    assert kept == [("2026-12-25", "2027-01-01", "day")]


def test_a_precision_that_does_not_match_the_yearless_form_is_dropped_as_bad_format():
    kept, drops = check("Hamburg conference Oct 15", [point("--10", "", "day", "Oct 15")])
    assert kept == [] and drops == {"time_bad_format": 1}


def test_a_yearless_date_with_no_recording_year_is_dropped():
    kept, drops = check("Hamburg conference Oct 15", [point("--10-15", "", "day", "Oct 15")], year=None)
    assert kept == [] and drops == {"time_year_unknown": 1}


def test_a_yearless_date_whose_source_prints_a_year_is_dropped():
    kept, drops = check("Hamburg conference Oct 15 2027", [point("--10-15", "", "day", "Oct 15 2027")])
    assert kept == [] and drops == {"time_yearless_but_year_printed": 1}


def test_a_yearless_date_the_source_does_not_hold_is_dropped():
    kept, drops = check("Hamburg conference Oct 15", [point("--11-15", "", "day", "Oct 15")])
    assert kept == [] and drops == {"time_date_not_in_source": 1}


def test_the_stored_answer_keeps_the_yearless_strings_so_another_year_re_fills():
    from src.domain.fact_profile_answer import ProfileAnswer
    answer = ProfileAnswer.model_validate(dict(
        label_words=[], time_role="event", time_points=[{"from": "--10-15", "to": "", "precision": "day",
                                                         "source": "Oct 15"}], places=[], people=[],
        tax_or_fine="none", user_identity=False, pet=False, state_or_law=False, about_person="none",
        subject="lifestyle", money_flow=False, use_areas=[], kinds=[], sensitivity="ordinary"))
    text = "Hamburg conference Oct 15"
    first = derive_profile(answer, text, None, PlaceNames([]), 2026)
    second = derive_profile(answer, text, None, PlaceNames([]), 2027)

    assert first.profile.time_points[0].start == "2026-10-15" and second.profile.time_points[0].start == "2027-10-15"
    assert first.profile.profile_answer["time_points"][0]["from"] == "--10-15"
    assert second.profile.profile_answer["time_points"][0]["from"] == "--10-15"


def test_a_full_date_is_checked_as_before_whatever_the_recording_year():
    kept, _ = check("Rome trip 10-14 Aug 2025", [point("2025-08-10", "2025-08-14", "day", "10-14 Aug 2025")])
    assert kept == [("2025-08-10", "2025-08-14", "day")]


# ------------------------------------------------------------------------------------------------ recording year

def test_the_recording_year_is_the_earlier_of_reported_and_created():
    created = datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert recording_year(datetime(2025, 12, 30, tzinfo=timezone.utc), created) == 2025
    assert recording_year(datetime(2026, 11, 9, tzinfo=timezone.utc), created) == 2026
    assert recording_year(created + timedelta(days=800), created) == 2026   # a later reported date never moves it


def test_naive_values_read_as_utc_and_a_zone_is_converted_to_utc():
    assert recording_year(datetime(2026, 1, 1), datetime(2026, 6, 1)) == 2026
    plus_two = timezone(timedelta(hours=2))
    assert recording_year(datetime(2026, 1, 1, 1, 0, tzinfo=plus_two), datetime(2026, 6, 1, tzinfo=plus_two)) == 2025

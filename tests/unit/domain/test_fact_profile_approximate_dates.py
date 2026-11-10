"""An approximate time around a printed date is filed at the coarser precision of that date (fact profile RFC §4.4);
a day worked out from it is dropped. Invented cast only."""
import pytest

from src.domain.drive_grade import TextEvidence
from src.domain.fact_profile_answer import AnswerTimePoint
from src.domain.fact_profile_derive import check_time_points

HOTEL = "The Hamburg conference talk was around mid-October 2026."


def point(start, precision, source, end=""):
    return AnswerTimePoint.model_validate({"from": start, "to": end, "precision": precision, "source": source})


def check(text, points):
    drops = {}
    kept = check_time_points(points, TextEvidence([text]), text.casefold(), [], drops, 2026)
    return [(p.start, p.precision) for p in kept], drops


@pytest.mark.parametrize("source", ["October 2026", "around mid-October 2026"])
def test_the_month_of_the_printed_date_is_accepted(source):
    assert check(HOTEL, [point("2026-10", "month", source)]) == ([("2026-10", "month")], {})


def test_the_year_of_the_printed_date_is_accepted():
    assert check(HOTEL, [point("2026", "year", "October 2026")]) == ([("2026", "year")], {})


def test_a_day_computed_from_it_is_dropped():
    assert check(HOTEL, [point("2026-10-01", "day", "October 2026")]) == ([], {"time_date_not_in_source": 1})


@pytest.mark.parametrize("text, source", [
    ("Hamburg conference talk around 15/10/2026.", "15/10/2026"),
    ("Hamburg conference talk around 15.10.2026.", "15.10.2026"),
    ("Hamburg conference talk around 2026-10-15.", "2026-10-15"),
])
def test_the_numeric_forms_of_the_printed_date_give_the_month_too(text, source):
    assert check(text, [point("2026-10", "month", source)]) == ([("2026-10", "month")], {})

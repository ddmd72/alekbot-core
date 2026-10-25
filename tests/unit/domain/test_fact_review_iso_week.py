"""Facts S4 · T4b review L2: the weekly digest's week is the ISO week of a calendar day, computed in the domain."""
from datetime import date, datetime, timezone

import pytest

from src.domain.fact_review_rules import iso_week_of


@pytest.mark.parametrize("day,week", [
    (date(2026, 10, 24), "2026-W43"),     # a Saturday
    (date(2026, 10, 19), "2026-W43"),     # the Monday of the same week
    (date(2026, 10, 25), "2026-W43"),     # its Sunday
    (date(2026, 10, 26), "2026-W44"),     # the next Monday
    (date(2026, 1, 5), "2026-W02"),
    (date(2027, 1, 1), "2026-W53"),       # 2026 has 53 ISO weeks: 1 January 2027 belongs to it
    (date(2024, 12, 30), "2025-W01"),     # a December day in the next ISO year
    (date(2021, 1, 3), "2020-W53"),
    (date(2025, 12, 28), "2025-W52"),     # 2025 has 52 weeks
    (date(999, 6, 1), "0999-W22"),        # the year is four digits
])
def test_the_iso_week_of_a_day(day, week):
    assert iso_week_of(day) == week


def test_a_datetime_is_not_a_day():
    with pytest.raises(ValueError):
        iso_week_of(datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc))

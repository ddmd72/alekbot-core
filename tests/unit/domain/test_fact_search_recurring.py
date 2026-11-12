"""Recurring occasions in the fact search (fact profile RFC OD-7): a label phrase containing birthday or anniversary
marks a fact whose short span matches a window in any year."""
from datetime import date

from src.domain.fact_search_lists import gap_recurring
from tests.support.fact_search_kit import fact, point, query, search

D = date.fromisoformat
NEXT_SUMMER = dict(intent="future", start=D("2027-06-01"), end=D("2027-08-31"))


def test_clara_birthday_in_july_reaches_a_next_summer_window_asked_in_november():
    clara = fact("clara", "Clara Moreira was born on 14 July 1982", points=[point("1982-07-14")],
                 labels=["birthday of clara"], domains=("family",))
    out = search([clara], query(**NEXT_SUMMER, domains=["family"]))
    assert out.lists["time"] == ["clara"] and out.kept == ["clara"]


def test_a_fact_that_is_not_an_occasion_is_not_projected():
    booking = fact("booking", points=[point("1982-07-14")], labels=["hotel booking"])
    assert search([booking], query(**NEXT_SUMMER)).lists["time"] == []


def test_an_anniversary_label_counts_and_the_phrase_may_hold_other_words():
    service = fact("service", points=[point("2000-07-01")], labels=["service anniversary party"])
    assert search([service], query(**NEXT_SUMMER)).lists["time"] == ["service"]


def test_a_leap_day_is_shifted_to_the_28th_in_a_year_without_one():
    leap = fact("leap", points=[point("2024-02-29")], labels=["anniversary"])
    window = dict(intent="period", start=D("2027-02-27"), end=D("2027-02-28"))
    assert search([leap], query(**window)).lists["time"] == ["leap"]


def test_a_span_over_31_days_is_not_projected():
    long_stay = fact("long", points=[point("2020-07-01", "2020-09-30")], labels=["birthday trip"])
    assert search([long_stay], query(**NEXT_SUMMER)).lists["time"] == []
    month = fact("month", points=[point("2020-07", precision="month")], labels=["birthday month"])
    assert search([month], query(**NEXT_SUMMER)).lists["time"] == ["month"]       # a month is 31 days at most


def test_a_year_precision_point_is_not_projected():
    year = fact("year", points=[point("1982", precision="year")], labels=["birthday"])
    assert search([year], query(**NEXT_SUMMER)).lists["time"] == []


def test_gap_recurring_is_the_nearest_projection_over_the_neighbouring_years():
    july = (D("1982-07-14"), D("1982-07-14"))
    assert gap_recurring(july, (D("2027-07-01"), D("2027-07-31"))) == 0
    assert gap_recurring(july, (D("2027-08-01"), D("2027-08-31"))) == 18
    # December and January are neighbours across the year boundary
    new_year = (D("2000-01-02"), D("2000-01-02"))
    assert gap_recurring(new_year, (D("2026-12-20"), D("2026-12-30"))) == 3


def test_now_order_and_latest_never_project_a_recurring_fact():
    # The kinds list keeps the time-based order (the domain list is ordered by closeness, list mode RFC §5).
    clara = fact("clara", points=[point("1982-07-14")], labels=["birthday"], kinds=["occasion"], role="event")
    recent = fact("recent", points=[point("2026-11-01")], role="event", kinds=["occasion"])
    out = search([clara, recent], query(kinds=["occasion"]))
    assert out.lists["kinds"] == ["recent", "clara"]
    latest = search([clara, recent], query(intent="latest", kinds=["occasion"]))
    assert latest.lists["kinds"] == ["recent", "clara"] and latest.lists["time"] == ["recent", "clara"]

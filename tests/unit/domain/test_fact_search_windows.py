"""Fact search windows and in-list order (fact profile RFC §5.3, OD-8): the target window per time intent, the distance to
it, and the order inside a key list with a window, for `latest`, and by time role without either."""
from datetime import date, timedelta

import pytest

from src.domain.fact_search_lists import gap, window
from tests.support.fact_search_kit import TODAY, fact, point, query, search

D = date.fromisoformat


@pytest.mark.parametrize("intent, start, end, expected", [
    ("none", None, None, None),
    ("latest", None, None, None),
    ("unheard-of", D("2027-01-01"), None, None),
    ("future", None, None, (TODAY, TODAY + timedelta(days=365))),
    ("future", D("2026-01-01"), None, (TODAY, TODAY + timedelta(days=365))),          # a start before today starts today
    ("future", D("2027-06-01"), None, (D("2027-06-01"), D("2028-05-31"))),             # a start after today is honoured
    ("future", D("2027-06-01"), D("2027-08-31"), (D("2027-06-01"), D("2027-08-31"))),
    ("future", D("2027-06-01"), D("2027-05-01"), (D("2027-06-01"), D("2028-05-31"))),  # an end before the start is invalid
    ("future", None, D("2026-12-31"), (TODAY, D("2026-12-31"))),
    ("past", None, None, (TODAY - timedelta(days=365), TODAY)),
    ("past", None, D("2027-01-01"), (TODAY - timedelta(days=365), TODAY)),             # the end is clamped to today
    ("past", D("2025-08-01"), D("2025-08-31"), (D("2025-08-01"), D("2025-08-31"))),
    ("past", D("2026-01-01"), D("2025-08-31"), (D("2024-08-31"), D("2025-08-31"))),    # a start after the end is invalid
    ("past", D("2025-08-01"), None, (D("2025-08-01"), TODAY)),
    ("period", D("2025-08-01"), D("2025-08-14"), (D("2025-08-01"), D("2025-08-14"))),
    ("period", D("2025-08-01"), None, (D("2025-08-01"), D("2025-09-01"))),             # one end: plus or minus 31 days
    ("period", None, D("2025-08-14"), (D("2025-07-14"), D("2025-08-14"))),
    ("period", D("2025-08-14"), D("2025-08-01"), (D("2025-08-14"), D("2025-09-14"))),  # reversed: the start alone
    ("period", None, None, None),
])
def test_window_per_intent(intent, start, end, expected):
    assert window(query(intent=intent, start=start, end=end)) == expected


@pytest.mark.parametrize("interval, span, days", [
    ((D("2025-08-10"), D("2025-08-14")), (D("2025-08-01"), D("2025-08-31")), 0),
    ((D("2025-08-10"), D("2025-08-14")), (D("2025-08-14"), D("2025-09-30")), 0),       # touching at one day
    ((D("2025-08-10"), D("2025-08-14")), (D("2025-09-01"), D("2025-09-30")), 18),      # before the window
    ((D("2025-08-10"), D("2025-08-14")), (D("2025-07-01"), D("2025-08-01")), 9),       # after the window
])
def test_gap_is_the_days_to_the_window(interval, span, days):
    assert gap(interval, span) == days


def test_with_a_window_a_key_list_is_ordered_by_distance_then_by_similarity():
    rome = fact("rome", points=[point("2025-08-10", "2025-08-14")], domains=("travel",))
    near = fact("near", points=[point("2025-07-20")], domains=("travel",))
    far = fact("far", points=[point("2024-01-05")], domains=("travel",))
    twin = fact("twin", points=[point("2025-08-12")], domains=("travel",))
    undated = fact("undated", domains=("travel",))
    out = search([far, undated, near, rome, twin], query(intent="past", start=D("2025-08-01"), end=D("2025-08-31"),
                                                         domains=["travel"]), sim={"twin": 0.9, "rome": 0.1})
    assert out.lists["domain"] == ["twin", "rome", "near", "far", "undated"]
    assert out.lists["time"] == ["twin", "rome"]


def test_a_fact_is_ordered_by_its_nearest_point():
    two = fact("two", points=[point("2020-01-01"), point("2025-08-20")], domains=("travel",))
    one = fact("one", points=[point("2025-07-01")], domains=("travel",))
    out = search([one, two], query(intent="past", start=D("2025-08-01"), end=D("2025-08-31"), domains=["travel"]))
    assert out.lists["domain"] == ["two", "one"]


def test_latest_orders_newest_first_and_puts_the_dated_facts_in_a_last_time_list():
    old = fact("old", points=[point("2020-03-01")], domains=("work",))
    new = fact("new", points=[point("2026-10-01")], domains=("work",))
    mid = fact("mid", points=[point("2024-05-05", "2024-06-30")], domains=("work",))
    undated = fact("undated", domains=("work",), reported=date(2026, 11, 1))
    out = search([old, new, mid, undated], query(intent="latest", domains=["work"]), semantic=["old", "undated"])
    assert out.lists["domain"] == ["undated", "new", "mid", "old"]       # an undated fact counts by its reported date
    assert list(out.lists)[-1] == "time" and out.lists["time"] == ["new", "mid", "old"]


def test_latest_adds_the_time_list_even_when_nothing_is_dated_but_never_without_a_list():
    assert search([fact("a")], query(intent="latest")).lists == {}
    out = search([fact("a")], query(intent="latest", domains=["health"]))
    assert out.lists == {"domain": ["a"], "time": []}


def test_without_a_window_state_and_event_facts_interleave_with_the_timeless_by_similarity():
    s1 = fact("s1", role="state", points=[point("2026-04-01")])
    s2 = fact("s2", role="validity", points=[point("2026-09-01", "2027-03-31")])
    e1 = fact("e1", role="event", points=[point("2026-11-01")])
    e2 = fact("e2", role="measurement", points=[point("2026-02-01")])
    e3 = fact("e3", role="event", reported=date(2026, 11, 5))          # undated: its reported date is the point
    t1 = fact("t1", role="timeless")
    t2 = fact("t2", role=None)
    out = search([s1, s2, e1, e2, e3, t1, t2], query(domains=["health"]), sim={"t1": 0.2, "t2": 0.7})
    # states newest first: s2 (ends 2027) before s1; events nearest today: e1 (8 d), e3 (4 d via reported)...
    assert out.lists["domain"] == ["s2", "e3", "t2", "s1", "e1", "t1", "e2"]


def test_event_order_breaks_a_distance_tie_by_the_newer_point():
    before = fact("before", role="event", points=[point("2026-11-04")])
    after = fact("after", role="event", points=[point("2026-11-14")])
    assert search([before, after], query(domains=["health"])).lists["domain"] == ["after", "before"]
    newer = fact("newer", role="event", points=[point("2026-11-14", "2026-11-20")])
    assert search([before, newer], query(domains=["health"])).lists["domain"] == ["newer", "before"]

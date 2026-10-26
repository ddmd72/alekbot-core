"""T9a: the janitor's rules over the pool and the review interval: the breaker, the cap, the plan, the daily task key, the
accounts a yesterday row sends the janitor to, and the waiting figure on the ops line."""
from datetime import date, datetime, timedelta, timezone

import pytest

from src.domain.due_fact import DueFact
from src.domain.entities import ExpiryKind
from src.domain.fact_lifecycle_day import REVIEW_FIGURES, CounterField, FactLifecycleDay
from src.domain.fact_lifecycle_sweep import BREAKER_BACKLOG, breaker_reason, daily_line, plan_sweep
from src.domain.fact_review_task_keys import daily_key

NOW = datetime(2026, 10, 23, 7, 30, tzinfo=timezone.utc)


def due(fact_id, days_ago=2):
    return DueFact(fact_id=fact_id, account_id="acc-1", expires_at=NOW - timedelta(days=days_ago),
                   expiry_kind=ExpiryKind.DATE, update_time=1)


# ------------------------------------------------------------------------------------------------ the breaker

@pytest.mark.parametrize("interval", [1, 3, 7])
def test_the_review_age_edge_is_the_interval_plus_72_hours_and_exclusive(interval):
    limit = timedelta(days=interval, hours=72)
    assert breaker_reason(3, 3, NOW - limit, NOW, interval) is None
    reason = breaker_reason(3, 3, NOW - limit - timedelta(seconds=1), NOW, interval)
    assert reason == f"3 stale facts and no successful review in the last {interval * 24 + 72} h"


def test_a_review_older_than_the_default_limit_is_fine_under_a_longer_interval():
    last = NOW - timedelta(days=5)
    assert breaker_reason(2, 2, last, NOW, 1) is not None
    assert breaker_reason(2, 2, last, NOW, 3) is None


def test_no_stale_fact_never_trips_on_age_but_a_big_pool_does():
    assert breaker_reason(0, 0, None, NOW, 1) is None
    assert breaker_reason(0, BREAKER_BACKLOG, None, NOW, 1) is None
    reason = breaker_reason(0, BREAKER_BACKLOG + 1, None, NOW, 1)
    assert reason is not None and "51" in reason and "50" in reason


def test_the_pool_edge_is_exclusive_and_a_fresh_review_does_not_lift_it():
    fresh = NOW - timedelta(hours=1)
    assert breaker_reason(10, 50, fresh, NOW, 1) is None
    assert breaker_reason(10, 51, fresh, NOW, 1) is not None


# ------------------------------------------------------------------------------------------------ the plan

def test_the_cap_is_the_pool_not_the_stale_backlog():
    plan = plan_sweep([due("a")], 5, {"q1"}, None, NOW, pool=45, last_review_at=NOW - timedelta(hours=1), interval=1)
    assert (plan.headroom, plan.breaker_reason, plan.deferred) == (5, None, 0)
    full = plan_sweep([due("a"), due("b")], 5, set(), None, NOW, pool=50, last_review_at=NOW, interval=1)
    assert (full.headroom, full.breaker_reason, full.deferred) == (0, None, 2)


def test_the_plan_hands_the_review_age_and_interval_to_the_breaker():
    last = NOW - timedelta(days=5)
    assert plan_sweep([], 1, set(), None, NOW, last_review_at=last, interval=1).breaker_reason is not None
    assert plan_sweep([], 1, set(), None, NOW, last_review_at=last, interval=3).breaker_reason is None


def test_a_pool_over_the_cap_trips_the_plan_and_leaves_no_headroom():
    plan = plan_sweep([due("a")], 10, set(), None, NOW, pool=60, last_review_at=NOW, interval=1)
    assert plan.breaker_reason is not None and plan.headroom == 0 and plan.deferred == 0


def test_without_a_pool_the_stale_backlog_alone_counts():
    assert plan_sweep([], 7, set(), None, NOW, last_review_at=NOW).headroom == BREAKER_BACKLOG - 7


# ------------------------------------------------------------------------------------------------ the daily key

@pytest.mark.parametrize("instant, expected", [
    (datetime(2026, 10, 23, 22, 58, tzinfo=timezone.utc), "fact-review:acc-1:2026-10-23"),      # 23:58 in Lisbon (summer time)
    (datetime(2026, 10, 23, 23, 3, tzinfo=timezone.utc), "fact-review:acc-1:2026-10-24"),       # 00:03 the next local day
    (datetime(2026, 12, 1, 23, 30, tzinfo=timezone.utc), "fact-review:acc-1:2026-12-01"),       # winter: Lisbon is UTC
])
def test_the_daily_key_names_the_owner_local_day(instant, expected):
    assert daily_key("acc-1", instant, "Europe/Lisbon") == expected


def test_two_accounts_have_two_keys_on_one_day():
    assert daily_key("acc-1", NOW, "UTC") != daily_key("acc-2", NOW, "UTC")


# ------------------------------------------------------------------------------------------------ yesterday's row

def test_the_review_figures_are_every_counter_but_the_expiry_tool_errors():
    assert set(REVIEW_FIGURES) == set(CounterField) - {CounterField.EXPIRY_TOOL_ERRORS}


@pytest.mark.parametrize("figure", REVIEW_FIGURES)
def test_any_review_counter_above_zero_is_a_review_figure(figure):
    assert FactLifecycleDay(account_id="a", day=date(2026, 10, 22), **{figure.value: 1}).has_review_figures()


def test_a_row_with_only_expiry_tool_errors_has_no_review_figure():
    assert not FactLifecycleDay(account_id="a", day=date(2026, 10, 22), expiry_tool_errors=4).has_review_figures()
    assert not FactLifecycleDay(account_id="a", day=date(2026, 10, 22)).has_review_figures()


# ------------------------------------------------------------------------------------------------ the line

def test_a_waiting_figure_alone_earns_a_line_and_shows_in_its_account_row():
    row = FactLifecycleDay(account_id="acc-12345678", day=date(2026, 10, 23))
    assert daily_line(NOW, [row], False) is None
    line = daily_line(NOW, [row], False, {"acc-12345678": 2})
    assert line is not None and line.endswith("| due_waiting 2")


def test_a_zero_waiting_figure_adds_nothing_to_the_row():
    row = FactLifecycleDay(account_id="acc-12345678", day=date(2026, 10, 23), due=1)
    assert "due_waiting" not in daily_line(NOW, [row], False, {"acc-12345678": 0})

"""T9p: the janitor's plan for one account (`plan_sweep`), the breaker labels and the one off-value parser."""
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.consolidation_pass_stats import stats_alerts_enabled
from src.domain.due_fact import DueFact
from src.domain.entities import ExpiryKind
from src.domain.env_switch import is_off_value
from src.domain.fact_lifecycle_sweep import (
    BREAKER_BACKLOG, BREAKER_OK, BREAKER_TRIPPED, BREAKER_WOULD_TRIP, SweepPlan, breaker_label, plan_sweep,
    stale_headroom, sweep_dry_run,
)

NOW = datetime(2026, 10, 23, 7, 30, tzinfo=timezone.utc)


def due(fact_id, days_ago, kind=ExpiryKind.DATE):
    return DueFact(fact_id=fact_id, account_id="acc-1", expires_at=NOW - timedelta(days=days_ago), expiry_kind=kind,
                   update_time=1)


def ids(plan):
    return [d.fact_id for d in plan.ordered]


# ------------------------------------------------------------------------------------------------ the plan

def test_every_field_of_a_plain_plan():
    dues = [due("b", 2), due("a", 5), due("n", 4, kind=None), due("w", 1)]
    plan = plan_sweep(dues, 3, {"w"}, NOW - timedelta(days=3), NOW)
    assert plan == SweepPlan(
        ordered=(dues[1], dues[0]),         # a (5 d) before b (2 d); n has no decision; w waits for its question
        no_decision=1,
        newly_due=1,                        # only b expired after the previous run (3 d ago); w is waiting, a and n are older
        due_waiting=1,
        headroom=BREAKER_BACKLOG - 3,
        breaker_reason="3 stale facts and no successful review in the last 72 h",
        deferred=0,                         # the breaker holds the run, so nothing is "left to the next one"
    )


def test_nothing_due_is_an_empty_plan():
    plan = plan_sweep([], 0, set(), None, NOW)
    assert plan == SweepPlan(ordered=(), no_decision=0, newly_due=0, due_waiting=0, headroom=BREAKER_BACKLOG,
                             breaker_reason=None, deferred=0)


def test_oldest_expiry_first_and_ties_by_id():
    dues = [due("c", 1), due("b", 3), due("a", 3), due("d", 9)]
    assert ids(plan_sweep(dues, 0, set(), None, NOW)) == ["d", "a", "b", "c"]


def test_the_order_does_not_depend_on_the_input_order():
    dues = [due("c", 1), due("b", 3), due("a", 3), due("d", 9)]
    assert ids(plan_sweep(list(reversed(dues)), 0, set(), None, NOW)) == ids(plan_sweep(dues, 0, set(), None, NOW))


def test_a_naive_expiry_is_read_as_utc():
    naive = DueFact(fact_id="n", account_id="acc-1", expires_at=(NOW - timedelta(days=1)).replace(tzinfo=None),
                    expiry_kind=ExpiryKind.DATE, update_time=1)
    assert ids(plan_sweep([naive, due("old", 2)], 0, set(), None, NOW)) == ["old", "n"]


def test_a_fact_without_a_decision_is_counted_and_never_ordered():
    plan = plan_sweep([due("a", 2), due("n1", 2, kind=None), due("n2", 3, kind=None)], 0, set(), None, NOW)
    assert ids(plan) == ["a"] and plan.no_decision == 2


@pytest.mark.parametrize("kind", [ExpiryKind.DATE, ExpiryKind.HORIZON, ExpiryKind.NONE])
def test_every_expiry_decision_is_ordered(kind):
    assert ids(plan_sweep([due("a", 2, kind=kind)], 0, set(), None, NOW)) == ["a"]


def test_due_waiting_are_the_due_facts_in_the_open_ids_and_leave_every_other_figure():
    dues = [due("a", 4), due("w1", 3), due("w2", 2, kind=None)]
    plan = plan_sweep(dues, 0, {"w1", "w2", "not-due"}, None, NOW)
    assert plan.due_waiting == 2                      # "not-due" is not a due fact: not counted
    assert ids(plan) == ["a"] and plan.no_decision == 0 and plan.newly_due == 1


def test_no_open_ids_leaves_nothing_waiting():
    plan = plan_sweep([due("a", 4), due("b", 3)], 0, frozenset(), None, NOW)
    assert plan.due_waiting == 0 and ids(plan) == ["a", "b"]


def test_newly_due_is_everything_without_a_previous_run():
    assert plan_sweep([due("a", 40), due("b", 3)], 0, set(), None, NOW).newly_due == 2


def test_newly_due_counts_only_expiries_after_the_previous_run():
    previous = NOW - timedelta(days=2)
    plan = plan_sweep([due("old", 3), due("edge", 2), due("new", 1)], 0, set(), previous, NOW)
    assert plan.newly_due == 1                        # the edge is exclusive


def test_newly_due_counts_a_fact_without_a_decision_too():
    assert plan_sweep([due("n", 1, kind=None)], 0, set(), NOW - timedelta(days=2), NOW).newly_due == 1


# ------------------------------------------------------------------------------------------------ headroom and breaker

@pytest.mark.parametrize("stale_count", [0, 1, 25, 49, 50, 51])
def test_the_headroom_is_the_domain_headroom(stale_count):
    assert plan_sweep([], stale_count, set(), None, NOW).headroom == stale_headroom(stale_count)


def test_no_backlog_means_no_breaker():
    assert plan_sweep([due("a", 1)], 0, set(), None, NOW).breaker_reason is None


def test_a_backlog_without_a_successful_review_trips_the_breaker():
    assert plan_sweep([due("a", 1)], 1, set(), None, NOW).breaker_reason is not None


def test_a_backlog_over_the_cap_trips_the_breaker():
    plan = plan_sweep([due("a", 1)], BREAKER_BACKLOG + 1, set(), None, NOW)
    assert plan.breaker_reason is not None and plan.headroom == 0


# ------------------------------------------------------------------------------------------------ deferred

def test_everything_that_fits_under_the_headroom_is_not_deferred():
    dues = [due(f"f{i}", 10 + i) for i in range(5)]
    assert plan_sweep(dues, 0, set(), None, NOW).deferred == 0


def test_deferred_over_the_cap_with_no_breaker():
    dues = [due(f"f{i:03d}", 100 - i) for i in range(BREAKER_BACKLOG + 7)]
    plan = plan_sweep(dues, 0, set(), None, NOW)
    assert plan.headroom == BREAKER_BACKLOG and len(plan.ordered) == BREAKER_BACKLOG + 7
    assert plan.deferred == 7


def test_a_held_breaker_defers_nothing_and_does_not_depend_on_the_mode():
    dues = [due(f"f{i}", 10 + i) for i in range(3)]
    plan = plan_sweep(dues, BREAKER_BACKLOG + 1, set(), None, NOW)
    assert plan.breaker_reason is not None and plan.deferred == 0 and len(plan.ordered) == 3


def test_the_log_only_deferred_is_the_write_paths_when_no_write_conflicts():
    """The write path stales `ordered` oldest first until `headroom` writes landed; the facts it never tried are the
    deferred ones. With no conflicting write that count is the plan's `deferred`, the figure log-only mode reports."""
    dues = [due(f"f{i:03d}", 100 - i) for i in range(BREAKER_BACKLOG + 7)]
    plan = plan_sweep(dues, 0, set(), None, NOW)
    staled = attempted = 0
    for _ in plan.ordered:                        # the service's loop, every write succeeding
        if staled >= plan.headroom:
            break
        attempted += 1
        staled += 1
    assert len(plan.ordered) - attempted == plan.deferred


def test_the_facts_of_open_questions_are_not_deferred_either():
    dues = [due(f"f{i:03d}", 100 - i) for i in range(BREAKER_BACKLOG + 2)]
    waiting = {d.fact_id for d in dues[:5]}
    plan = plan_sweep(dues, 0, waiting, None, NOW)
    assert plan.due_waiting == 5 and plan.deferred == 0 and len(plan.ordered) == BREAKER_BACKLOG - 3


# ------------------------------------------------------------------------------------------------ labels

def test_the_labels_are_the_words_the_snapshot_and_the_line_carry():
    assert (BREAKER_OK, BREAKER_TRIPPED, BREAKER_WOULD_TRIP) == ("ok", "tripped", "would trip")


@pytest.mark.parametrize("reason,dry_run,expected", [
    (None, False, BREAKER_OK), (None, True, BREAKER_OK),
    ("why", False, BREAKER_TRIPPED), ("why", True, BREAKER_WOULD_TRIP),
])
def test_the_breaker_label(reason, dry_run, expected):
    assert breaker_label(reason, dry_run) == expected


# ------------------------------------------------------------------------------------------------ is_off_value

@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "FALSE", "No", "OfF", "  off  ", " 0", "False\n"])
def test_an_explicit_off_value_is_off(raw):
    assert is_off_value(raw) is True


@pytest.mark.parametrize("raw", [None, "", "  ", "1", "true", "yes", "on", "offf", "00", "nope", "disabled"])
def test_anything_else_is_not_an_off_value(raw):
    assert is_off_value(raw) is False


@pytest.mark.parametrize("raw", [None, "", "1", "true", "garbage", "0", "false", "no", "off", " OFF "])
def test_both_switches_read_the_one_parser(raw):
    assert sweep_dry_run(raw) is (not is_off_value(raw))
    assert stats_alerts_enabled(raw or "") is (not is_off_value(raw))

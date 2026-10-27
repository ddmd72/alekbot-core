"""T9b: the janitor's alert rules and the review's part of the ops line (plan §3.11): each condition and its edge, the repeat
rules, the one message per account, the line segments from the row's counters and the run's question figures."""
from datetime import date, timedelta

import pytest

from src.domain.fact_lifecycle_alerts import (
    EXPIRY_TOOL_ERRORS_ALERT, account_message, breaker_alert, expiry_errors_alert, purge_alert, review_alerts,
)
from src.domain.fact_lifecycle_day import REVIEW_FIGURES, CounterField, FactLifecycleDay
from src.domain.fact_lifecycle_review_line import (
    LINE_COUNTERS, QuestionStanding, has_review_line, question_standing, review_segments,
)
from src.domain.fact_lifecycle_sweep import daily_line
from src.domain.fact_review_question import QuestionStatus
from tests.support.review_rule_builders import NOW, question, state

ACC = "acc-12345678"


def alerts(*, pool=0, oldest_days=None, interval=1, st=None, questions=(), failed=0):
    oldest = None if oldest_days is None else NOW - timedelta(days=oldest_days)
    return review_alerts(pool=pool, oldest_stale=oldest, interval=interval, state=st or state(), questions=questions,
                         reply_intake_failed=failed, now=NOW)


def queued(count):
    return [question(QuestionStatus.QUEUED, question_id=f"q{i}", fact_id=f"f{i}") for i in range(count)]


# ------------------------------------------------------------------------------------------------ the pool

def test_the_pool_alert_is_over_fifty_and_exclusive():
    assert alerts(pool=50).lines == ()
    (line,) = alerts(pool=51).lines
    assert "51" in line and "50" in line


# ------------------------------------------------------------------------------------------------ the oldest stale fact

@pytest.mark.parametrize("interval", [1, 3])
def test_the_oldest_stale_alert_is_older_than_the_interval_plus_two_days_and_exclusive(interval):
    limit = interval + 2
    assert alerts(interval=interval, oldest_days=limit).record_oldest is False
    found = alerts(interval=interval, oldest_days=limit + 0.01)
    assert found.record_oldest is True and len(found.lines) == 1 and f"limit {limit}" in found.lines[0]


def test_a_stale_fact_without_an_age_raises_nothing():
    assert alerts(oldest_days=None) == alerts()


def test_the_oldest_stale_alert_repeats_weekly_not_sooner():
    waiting = dict(interval=1, oldest_days=20)
    assert alerts(**waiting, st=state(oldest_alert_at=NOW - timedelta(days=6, hours=23))).lines == ()
    again = alerts(**waiting, st=state(oldest_alert_at=NOW - timedelta(days=7)))
    assert again.record_oldest is True and len(again.lines) == 1


def test_an_oldest_alert_mark_is_cleared_when_the_condition_stops_holding():
    found = alerts(interval=1, oldest_days=2, st=state(oldest_alert_at=NOW - timedelta(days=30)))
    assert (found.lines, found.record_oldest, found.rearm_oldest) == ((), False, True)
    assert alerts(oldest_days=None, st=state(oldest_alert_at=NOW)).rearm_oldest is True


def test_there_is_nothing_to_rearm_without_a_mark_and_nothing_while_the_episode_goes_on():
    assert alerts(interval=1, oldest_days=2).rearm_oldest is False
    running = alerts(interval=1, oldest_days=20, st=state(oldest_alert_at=NOW - timedelta(days=2)))
    assert (running.lines, running.record_oldest, running.rearm_oldest) == ((), False, False)


# ------------------------------------------------------------------------------------------------ the queue

def test_the_queue_alert_is_over_35_once_and_exclusive():
    assert alerts(questions=queued(35)).queue is None
    found = alerts(questions=queued(36))
    assert found.queue == "raise" and "36" in found.lines[0]


def test_a_raised_queue_alert_is_silent_while_over_and_rearms_at_35_or_fewer():
    raised = state(queue_alert_at=NOW - timedelta(days=1))
    assert alerts(questions=queued(37), st=raised) == alerts()
    found = alerts(questions=queued(35), st=raised)
    assert (found.queue, found.lines) == ("rearm", ())


def test_only_queued_questions_count_toward_the_queue():
    asked = [question(QuestionStatus.ASKED, question_id=f"a{i}", fact_id=f"fa{i}") for i in range(40)]
    assert alerts(questions=asked).queue is None


# ------------------------------------------------------------------------------------------------ 48 hours and replies

def test_no_successful_review_for_more_than_48_hours_and_exclusive():
    assert alerts(st=state(unsuccessful_since=NOW - timedelta(hours=48))).lines == ()
    (line,) = alerts(st=state(unsuccessful_since=NOW - timedelta(hours=48, seconds=1))).lines
    assert "48 h" in line
    assert alerts(st=state()).lines == ()


def test_it_repeats_on_every_run_while_it_holds():
    st = state(unsuccessful_since=NOW - timedelta(days=5))
    assert alerts(st=st).lines == alerts(st=st).lines != ()


def test_replies_not_recorded_yesterday_raise_one_line_with_the_count():
    assert alerts(failed=0).lines == ()
    (line,) = alerts(failed=3).lines
    assert "3" in line


def test_the_conditions_add_up_in_a_fixed_order():
    found = alerts(pool=60, interval=1, oldest_days=9, questions=queued(40), failed=1,
                   st=state(unsuccessful_since=NOW - timedelta(days=3)))
    assert [line.split()[0:2] for line in found.lines] == [
        ["the", "pool"], ["the", "oldest"], ["40", "questions"], ["no", "successful"], ["1", "owner"],
    ]


# ------------------------------------------------------------------------------------------------ the expiry errors and the breaker

def test_expiry_tool_errors_alert_above_ten_only():
    assert expiry_errors_alert(EXPIRY_TOOL_ERRORS_ALERT) is None
    assert "11" in expiry_errors_alert(EXPIRY_TOOL_ERRORS_ALERT + 1)


def test_the_breaker_alone_keeps_its_sentence():
    assert account_message(ACC, breaker_alert(ACC, "pool of 51 is over 50", 3), []) == (
        "🚨 Fact lifecycle breaker tripped for account acc-1234: pool of 51 is over 50. 3 due facts were not staled."
    )


def test_one_message_carries_the_breaker_and_the_conditions():
    message = account_message(ACC, breaker_alert(ACC, "r", 1), ["the pool is 60"])
    assert message.splitlines() == [
        "🚨 Fact lifecycle, account acc-1234:", "- breaker tripped for account acc-1234: r. 1 due facts were not staled.",
        "- the pool is 60",
    ]
    assert account_message(ACC, None, ["a"]).splitlines() == ["🚨 Fact lifecycle, account acc-1234:", "- a"]
    assert account_message(ACC, None, []) is None


def test_the_purge_alert_names_the_count():
    assert "7" in purge_alert(7) and purge_alert(7).startswith("🚨")


# ------------------------------------------------------------------------------------------------ the counters

def test_the_review_figures_list_every_counter_but_the_expiry_ones_once_and_start_with_the_line_counters():
    assert len(REVIEW_FIGURES) == len(set(REVIEW_FIGURES))
    assert set(REVIEW_FIGURES) == set(CounterField) - {CounterField.EXPIRY_TOOL_ERRORS}
    assert REVIEW_FIGURES[:len(LINE_COUNTERS)] == LINE_COUNTERS


# ------------------------------------------------------------------------------------------------ the line segments

def row(**over):
    return FactLifecycleDay(account_id=ACC, day=date(2026, 10, 23), **over)


def test_a_row_with_no_review_figure_has_no_segment():
    assert review_segments(row(review_enqueued=1), 0, QuestionStanding()) == ""
    assert not has_review_line(row(review_enqueued=1), 0, QuestionStanding())


def test_the_segments_come_in_the_plans_order_with_every_figure_of_a_segment_shown():
    full = row(reviewed=5, revived=1, rewritten=1, archived_by_review=1, invalidated=0, superseded=0, kept=2, queued=3,
               questions_sent=4, answered_by_digit=2, answered_in_cabinet=1, closed_by_reply=1, timed_out=1,
               review_failed=1, review_refusals=2, reply_intake_failed=3, review_enqueued=1)
    text = review_segments(full, 2, QuestionStanding(queued=6, unanswered=1))
    assert text == (
        " | review: reviewed 5; counted on their own: revived 1, rewritten 1, archived 1, invalidated 0, superseded 0,"
        " kept 2, queued 3, to history 0, folded 0"
        " | questions sent 4, answered by digit 2, by button 0, in the Cabinet 1, by reply 1, timed out 1,"
        " queue 6, unanswered 1, due_waiting 2"
        " | review failed 1, refusals 2, fallbacks not applied 0"
        " | replies not recorded 3"
        " | enqueued 1"
    )


def test_the_review_wording_does_not_call_its_figures_a_breakdown_of_reviewed():
    # kept includes timed-out candidates (also in `timed out`), queued includes kept-twice questions outside `reviewed`
    text = review_segments(row(reviewed=2, kept=2, queued=3, timed_out=1), 0, QuestionStanding())
    assert "reviewed 2; counted on their own:" in text and "kept 2" in text and "queued 3" in text
    assert "(" not in text


@pytest.mark.parametrize("over, present, absent", [
    ({"reviewed": 1}, "review: reviewed 1", "questions sent"),
    ({"timed_out": 1}, "timed out 1", "review: reviewed"),
    ({"review_refusals": 1}, "review failed 0, refusals 1", "replies not recorded"),
    ({"reply_intake_failed": 1}, "replies not recorded 1", "review failed"),
    ({"answered_by_button": 1}, "by button 1", "review failed"),
])
def test_a_segment_appears_only_when_one_of_its_figures_is_above_zero(over, present, absent):
    text = review_segments(row(**over), 0, QuestionStanding())
    assert present in text and absent not in text and text.endswith(" | enqueued 0")


@pytest.mark.parametrize("standing, waiting", [(QuestionStanding(queued=1), 0), (QuestionStanding(unanswered=1), 0),
                                              (QuestionStanding(), 1)])
def test_the_runs_question_figures_alone_earn_the_questions_segment(standing, waiting):
    text = review_segments(row(), waiting, standing)
    assert text.startswith(" | questions sent 0,") and "review:" not in text


def test_the_counters_the_line_does_not_print_earn_no_segment():
    unprinted = set(REVIEW_FIGURES) - set(LINE_COUNTERS)
    assert unprinted == {
        CounterField.NOT_DELIVERED, CounterField.ANSWERS_NOT_APPLIED, CounterField.REPLIES_IN_WORDS,
        CounterField.ALREADY_ANSWERED, CounterField.LABELS_FALLBACK,
    }
    for counter in unprinted:
        assert review_segments(row(**{counter.value: 4}), 0, QuestionStanding()) == ""


def test_question_standing_counts_queued_and_unanswered_only():
    held = [question(QuestionStatus.QUEUED, question_id="a", fact_id="fa"),
            question(QuestionStatus.QUEUED, question_id="b", fact_id="fb"),
            question(QuestionStatus.UNANSWERED, question_id="c", fact_id="fc"),
            question(QuestionStatus.ASKED, question_id="d", fact_id="fd")]
    assert question_standing(held) == QuestionStanding(queued=2, unanswered=1)


# ------------------------------------------------------------------------------------------------ the whole line

def test_the_account_row_gains_the_segments_after_the_expiry_errors():
    line = daily_line(NOW, [row(due=1, reviewed=1)], False)
    account = line.splitlines()[1]
    assert account.index("expiry tool errors yesterday 0") < account.index("| review: reviewed 1")


def test_the_purge_line_sits_under_the_header_and_alone_earns_a_line():
    quiet = daily_line(NOW, [row()], False, purge_overdue=4)
    assert quiet.splitlines() == ["🧹 Fact lifecycle — run 2026-10-24 09:00 UTC", "purge overdue 4"]
    both = daily_line(NOW, [row(due=1)], True, purge_overdue=4).splitlines()
    assert both[0].endswith("· log-only") and both[1] == "purge overdue 4" and both[2].startswith("acc-1234")
    assert daily_line(NOW, [row()], False, purge_overdue=0) is None

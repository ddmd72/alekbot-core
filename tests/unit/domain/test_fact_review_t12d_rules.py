"""Rules moved into the domain by T12d (hexagonal review S-M1, S-M2, S-M4, S-L1, S-L2, S-L4, S-L5, S-L6, S-L7, S-L9, D-L5)."""
from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.domain.fact_lifecycle_alerts import review_alerts, revive_alert, stale_age
from src.domain.fact_lifecycle_day import FactLifecycleDay, counter_day
from src.domain.fact_lifecycle_sweep import accounts_to_visit, unvisited_rows
from src.domain.fact_answer_result import AnswerResult
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_answers import answer_closes_fact, option_answer, option_id
from src.domain.fact_review_language import LABEL_KEYS, is_review_text
from src.domain.fact_review_question import QuestionStatus
from src.domain.fact_review_reply import ClickPath, ClickShow, click_outcome, click_path
from src.domain.fact_review_rules import fallback_revives, fallback_sets_aside
from src.domain.fact_review_state import FactReviewState
from src.domain.fact_review_task import FactReviewTask, ReviewItem, decode_review_task, encode_review_task
from src.domain.fact_review_task_keys import REVIEW_DEADLINE_S
from src.domain.messaging import MessageRef
from src.domain.ui_messages import UIMessage
from tests.support.review_rule_builders import NOW, REF, fact, question

OTHER_REF = MessageRef(platform="slack", channel_id="D1", message_id="1729760999.000900")


# ------------------------------------------------------------------------------------------------ the counter day

def test_a_counters_day_is_the_utc_date_of_the_event():
    late_in_lisbon_winter = datetime(2026, 12, 1, 23, 30, tzinfo=timezone(timedelta(hours=-3)))
    assert counter_day(late_in_lisbon_winter) == date(2026, 12, 2)
    assert counter_day(datetime(2026, 12, 1, 23, 30)) == date(2026, 12, 1)


def test_the_review_task_deadline_is_one_constant():
    assert REVIEW_DEADLINE_S == 1800


# ------------------------------------------------------------------------------------------------ clicks

@pytest.mark.parametrize("status", [QuestionStatus.ASKED, QuestionStatus.UNANSWERED])
def test_an_open_question_takes_a_click_only_on_its_own_message(status):
    asked = question(status)
    assert click_path(asked, REF) is ClickPath.OPEN
    assert click_path(asked, OTHER_REF) is ClickPath.IGNORED


def test_a_queued_question_has_no_message_so_no_click_is_its():
    assert click_path(question(QuestionStatus.QUEUED), REF) is ClickPath.IGNORED


def test_a_closed_question_shows_its_answer_on_its_message_or_when_it_has_no_ref():
    closed = question(QuestionStatus.CLOSED)
    assert click_path(closed, REF) is ClickPath.CLOSED
    assert click_path(closed, OTHER_REF) is ClickPath.IGNORED
    assert click_path(closed.model_copy(update={"message_ref": None}), OTHER_REF) is ClickPath.CLOSED


def test_each_apply_result_shows_one_thing_on_the_message():
    assert click_outcome(AnswerResult.APPLIED).show is ClickShow.CHOSEN
    for result in (AnswerResult.CHANGED, AnswerResult.CONFLICT):
        outcome = click_outcome(result)
        assert (outcome.show, outcome.note) == (ClickShow.KEPT, UIMessage.MEMORY_FACT_CHANGED)
    assert click_outcome(AnswerResult.NOT_OPEN).show is ClickShow.CLOSED
    for result in (AnswerResult.NOT_APPLIED, AnswerResult.NOT_FOUND):
        outcome = click_outcome(result)
        assert (outcome.show, outcome.note) == (ClickShow.NOTHING, None)


def test_every_apply_result_is_decided():
    for result in AnswerResult:
        assert click_outcome(result).show in ClickShow


# ------------------------------------------------------------------------------------------------ options and texts

@pytest.mark.parametrize("kind", list(AnswerKind))
def test_an_option_id_reads_back_as_its_answer(kind):
    assert option_answer(option_id(kind)) is kind


def test_a_review_text_is_one_of_the_label_keys():
    assert all(is_review_text(UIMessage(key)) for key in LABEL_KEYS)


def test_a_review_text_outside_the_labels_is_refused():
    outside = next(message for message in UIMessage if message.value not in LABEL_KEYS)
    assert not is_review_text(outside)


@pytest.mark.parametrize("fallback", ["revive", "archive"])
def test_a_fallback_sets_aside_exactly_when_it_does_not_revive(fallback):
    asked = question(QuestionStatus.ASKED, fallback=fallback)
    assert fallback_sets_aside(asked) is (not fallback_revives(asked))
    assert fallback_sets_aside(asked) is (fallback == "archive")


@pytest.mark.parametrize("fallback", ["revive", "archive"])
def test_dont_know_closes_the_fact_exactly_when_the_fallback_sets_it_aside(fallback):
    asked = question(QuestionStatus.ASKED, fallback=fallback)
    assert answer_closes_fact(AnswerKind.DONT_KNOW, asked) is fallback_sets_aside(asked)


# ------------------------------------------------------------------------------------------------ the janitor

def _row(account_id, **over):
    return FactLifecycleDay(account_id=account_id, day=date(2026, 10, 23), **over)


def test_the_accounts_to_visit_are_every_source_once_in_id_order():
    visited = accounts_to_visit(
        with_due=["b"], with_backlog=["a", "b"], with_state=["d"], with_questions=["c"],
        yesterday=[_row("e", reviewed=2), _row("f", expiry_tool_errors=3), _row("g")],
    )
    assert visited == ["a", "b", "c", "d", "e"]


def test_an_account_with_only_refused_expiry_calls_yesterday_is_not_visited_but_gets_a_row():
    yesterday = [_row("e", reviewed=2), _row("f", expiry_tool_errors=3), _row("g"), _row("h", expiry_tool_errors=1)]
    rows = unvisited_rows(yesterday, {"h", "e"}, date(2026, 10, 24))
    assert [(r.account_id, r.day, r.expiry_tool_errors) for r in rows] == [("f", date(2026, 10, 24), 3)]


def test_the_oldest_stale_age_never_goes_below_zero():
    assert stale_age(NOW - timedelta(days=3, hours=5), NOW) == timedelta(days=3, hours=5)
    assert stale_age(NOW + timedelta(days=1), NOW) == timedelta(0)


def test_the_oldest_stale_alert_reads_the_same_age():
    alerts = review_alerts(
        pool=1, oldest_stale=NOW - timedelta(days=20), interval=7, state=FactReviewState(account_id="acc"),
        questions=[], reply_intake_failed=0, now=NOW,
    )
    assert alerts.lines == ("the oldest stale fact has waited 20 days (limit 9)",)


def test_the_revive_alert_names_the_window_and_each_fact_with_its_count():
    text = revive_alert("acc-1234567890", [("fact-aaaa-1111", 3), ("fact-bbbb-2222", 4)])
    assert text == (
        "🚨 [FactReview] account acc-1234: revived again within 30 days, the horizon is probably wrong: "
        "fact-aaa (3×), fact-bbb (4×)"
    )


# ------------------------------------------------------------------------------------------------ the review task's instant

def _task(**over):
    stale = fact("f1")
    return FactReviewTask(items=(ReviewItem(fact=stale),), mirror=False, **over)


def test_a_review_task_carries_the_runs_instant_through_its_plain_form():
    task = _task(now=NOW)
    assert decode_review_task(encode_review_task(task)) == task
    assert decode_review_task(encode_review_task(task)).now == NOW


def test_a_review_task_without_an_instant_has_none():
    assert _task().now is None


def test_the_instant_must_be_timezone_aware():
    with pytest.raises(ValidationError):
        _task(now=datetime(2026, 10, 24, 9, 0))


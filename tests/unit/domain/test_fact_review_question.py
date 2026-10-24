"""A memory-review question and the account's review state (Facts S4 · T1a, plan §2.2, §3.1, §3.7, §3.8)."""
from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.domain.fact_review_answers import AnswerKind
from src.domain.fact_review_question import (
    MAX_REPLY_CHARS,
    QUESTION_TIMEOUT,
    ClosedBy,
    FactReviewQuestion,
    QuestionStatus,
)
from src.domain.fact_review_state import FactReviewState
from src.domain.messaging import MessageRef

UTC = timezone.utc
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
ASKED = NOW - timedelta(days=3)
REF = MessageRef(platform="slack", channel_id="D1", message_id="1729760000.000100")


def question(status=QuestionStatus.QUEUED, **over):
    fields = dict(
        question_id="q1", account_id="acc", user_id="u1", fact_id="f1", quoted_text="Lives in Lisbon",
        fallback="revive", queue_key=NOW - timedelta(days=30), queued_at=NOW - timedelta(days=2), status=status,
    )
    if status in (QuestionStatus.ASKED, QuestionStatus.UNANSWERED):
        fields.update(asked_at=ASKED, message_ref=REF)
    if status is QuestionStatus.CLOSED:
        fields.update(asked_at=ASKED, closed_at=NOW, closed_by=ClosedBy.OWNER, answer=AnswerKind.NEVER,
                      answer_source="digit", purge_after=NOW + timedelta(days=90))
    fields.update(over)
    return FactReviewQuestion(**fields)


# ---------------------------------------------------------------------------------------------------------- statuses

def test_the_statuses_and_closers_are_the_ones_the_plan_names():
    assert [s.value for s in QuestionStatus] == ["queued", "pending", "asked", "unanswered", "closed"]
    assert [c.value for c in ClosedBy] == ["review", "timeout", "owner", "resolved_elsewhere"]
    assert [a.value for a in AnswerKind] == ["still_true", "ended", "never", "dont_know"]


@pytest.mark.parametrize("status,is_open", [
    (QuestionStatus.QUEUED, True), (QuestionStatus.PENDING, True), (QuestionStatus.ASKED, True),
    (QuestionStatus.UNANSWERED, True), (QuestionStatus.CLOSED, False),
])
def test_only_a_closed_question_is_not_open(status, is_open):
    assert question(status).is_open is is_open


def test_a_new_question_is_queued_with_no_message_yet():
    q = question()
    assert q.status is QuestionStatus.QUEUED and q.message_ref is None and q.asked_at is None and not q.noise


# ---------------------------------------------------------------------------------------------------------- timeouts

def test_the_timeout_is_fourteen_days():
    assert QUESTION_TIMEOUT == timedelta(days=14)


@pytest.mark.parametrize("status", [QuestionStatus.ASKED, QuestionStatus.UNANSWERED])
def test_a_sent_question_times_out_fourteen_days_after_it_was_asked(status):
    q = question(status, asked_at=NOW - QUESTION_TIMEOUT + timedelta(seconds=1))
    assert not q.is_timed_out(NOW)
    assert q.is_timed_out(NOW + timedelta(seconds=1))


@pytest.mark.parametrize("status", [QuestionStatus.ASKED, QuestionStatus.UNANSWERED])
def test_the_moment_fourteen_days_are_full_the_question_has_timed_out(status):
    assert question(status, asked_at=NOW - QUESTION_TIMEOUT).is_timed_out(NOW)


@pytest.mark.parametrize("status", [QuestionStatus.ASKED, QuestionStatus.UNANSWERED])
def test_a_reply_never_shortens_the_wait(status):
    q = question(status, reply_text="yes, still", reply_at=NOW - timedelta(days=13), asked_at=NOW - timedelta(days=13))
    assert not q.is_timed_out(NOW)


def test_a_queued_question_never_times_out_however_old():
    q = question(queued_at=NOW - timedelta(days=900), queue_key=NOW - timedelta(days=1000))
    assert not q.is_timed_out(NOW + timedelta(days=5000))


def test_a_pending_question_has_not_been_asked_and_does_not_time_out():
    assert not question(QuestionStatus.PENDING, session_id="u1:D1").is_timed_out(NOW + timedelta(days=60))


def test_a_closed_question_does_not_time_out():
    assert not question(QuestionStatus.CLOSED).is_timed_out(NOW + timedelta(days=60))


# ---------------------------------------------------------------------------------------------------------- replies

def test_a_recorded_reply_is_not_shown_until_the_review_has_offered_it():
    q = question(QuestionStatus.ASKED, reply_text="it is Y now", reply_at=NOW)
    assert q.reply_not_shown
    assert not question(QuestionStatus.ASKED, reply_text="it is Y now", reply_at=NOW, offered_at=NOW).reply_not_shown


@pytest.mark.parametrize("reply", [None, ""])
def test_no_reply_text_is_nothing_to_show(reply):
    assert not question(QuestionStatus.ASKED, reply_text=reply).reply_not_shown


def test_a_closed_question_has_no_reply_waiting_for_the_review():
    assert not question(QuestionStatus.CLOSED, reply_text="late words").reply_not_shown


def test_a_reply_is_bounded():
    question(QuestionStatus.ASKED, reply_text="x" * MAX_REPLY_CHARS)
    with pytest.raises(ValidationError):
        question(QuestionStatus.ASKED, reply_text="x" * (MAX_REPLY_CHARS + 1))


# ---------------------------------------------------------------------------------------------------------- shape

def test_a_sent_question_must_say_when_it_was_asked():
    for status in (QuestionStatus.ASKED, QuestionStatus.UNANSWERED):
        with pytest.raises(ValidationError, match="asked_at"):
            question(status, asked_at=None)


def test_a_closed_question_says_when_and_by_whom():
    with pytest.raises(ValidationError, match="closed_at and closed_by"):
        question(QuestionStatus.CLOSED, closed_by=None)
    with pytest.raises(ValidationError, match="closed_at and closed_by"):
        question(QuestionStatus.CLOSED, closed_at=None)


@pytest.mark.parametrize("field,value", [
    ("closed_at", NOW), ("closed_by", ClosedBy.REVIEW), ("purge_after", NOW), ("answer", AnswerKind.ENDED),
    ("answer_source", "cabinet"),
])
def test_only_a_closed_question_carries_a_closing(field, value):
    with pytest.raises(ValidationError, match="only a closed question"):
        question(QuestionStatus.ASKED, **{field: value})


def test_a_closed_question_without_an_answer_is_a_timeout_or_a_review_close():
    q = question(QuestionStatus.CLOSED, closed_by=ClosedBy.TIMEOUT, answer=None, answer_source=None)
    assert not q.is_open and q.answer is None


def test_an_answer_must_be_one_of_the_four_and_its_source_one_of_the_four_ways():
    with pytest.raises(ValidationError):
        question(QuestionStatus.CLOSED, answer="maybe")
    with pytest.raises(ValidationError):
        question(QuestionStatus.CLOSED, answer_source="telepathy")
    assert question(QuestionStatus.CLOSED, answer="ended", answer_source="button").answer is AnswerKind.ENDED


def test_the_fallback_is_revive_or_archive():
    with pytest.raises(ValidationError):
        question(fallback="invalidate")
    assert question(fallback="archive", fallback_date=date(2026, 9, 1)).fallback_date == date(2026, 9, 1)


def test_the_queue_key_is_required():
    with pytest.raises(ValidationError):
        FactReviewQuestion(question_id="q", account_id="a", user_id="u", fact_id="f", quoted_text="t",
                           fallback="revive", queued_at=NOW)


def test_ids_cannot_be_empty():
    for field in ("question_id", "account_id", "user_id", "fact_id"):
        with pytest.raises(ValidationError):
            question(**{field: ""})


def test_the_question_round_trips_through_its_dump_with_the_message_ref():
    q = question(QuestionStatus.ASKED, reply_text="words", reply_at=NOW, noise=True)
    assert FactReviewQuestion.model_validate(q.model_dump()) == q
    assert FactReviewQuestion.model_validate_json(q.model_dump_json()) == q
    assert q.message_ref == REF


def test_the_question_is_a_value_that_does_not_change_in_place():
    with pytest.raises(ValidationError):
        question().status = QuestionStatus.CLOSED


# ---------------------------------------------------------------------------------------------------------- state

def test_a_fresh_state_has_nothing_open_nothing_sent_and_no_labels():
    state = FactReviewState(account_id="acc")
    assert state.current_question_id is None and state.sent == 0 and state.sent_on is None
    assert state.delivery_failed_on is None and state.message_language is None and state.queue_alert_at is None
    assert state.offers == {} and state.labels == {}


def test_the_state_defaults_are_not_shared_between_states():
    first, second = FactReviewState(account_id="a"), FactReviewState(account_id="b")
    first.offers["f1"] = 2
    first.labels["pl"] = {"question": "Czy to nadal prawda?"}
    assert second.offers == {} and second.labels == {}


def test_the_state_keeps_the_days_and_the_labels_it_was_given():
    state = FactReviewState(
        account_id="acc", current_question_id="q1", sent_on=date(2026, 10, 24), sent=3,
        delivery_failed_on=date(2026, 10, 23), message_language="pl", offers={"f1": 1},
        labels={"pl": {"question": "Czy to nadal prawda?"}}, last_review_at=NOW,
    )
    assert FactReviewState.model_validate(state.model_dump()) == state
    assert state.labels["pl"]["question"] == "Czy to nadal prawda?"


def test_the_sent_count_cannot_be_negative_and_the_account_is_required():
    with pytest.raises(ValidationError):
        FactReviewState(account_id="acc", sent=-1)
    with pytest.raises(ValidationError):
        FactReviewState(account_id="")

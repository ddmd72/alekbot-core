"""Facts S4 · T11a: what the owner can look at of the memory review (`fact_review_overview`): the listable states, the window of
the lately set-aside list, whose questions a user sees, and the order and status of the open questions."""
from datetime import timedelta

import pytest

from src.domain.fact_review_overview import listable_state, queue_entries, set_aside_since, user_questions
from src.domain.fact_review_question import QuestionStatus
from src.domain.fact_review_state import FactReviewState
from tests.support.review_rule_builders import NOW, fact, question, state


@pytest.mark.parametrize("raw, expected", [
    (None, "current"), ("", "current"), ("current", "current"), ("stale", "stale"),
    ("archived", None), ("superseded", None), ("Stale", None), ("all", None),
])
def test_listable_state(raw, expected):
    assert listable_state(raw) == expected


def test_the_set_aside_list_starts_thirty_days_back():
    assert set_aside_since(NOW) == NOW - timedelta(days=30)


def test_a_user_sees_only_his_own_open_questions_of_his_account():
    mine = question(question_id="mine")
    other_user = question(question_id="colleague", user_id="u2")
    other_account = question(question_id="stranger", account_id="acc-2")
    closed = question(QuestionStatus.CLOSED, question_id="done")

    assert [q.question_id for q in user_questions([mine, other_user, other_account, closed], "acc", "u1")] == ["mine"]


def test_entries_are_current_then_sent_then_the_queue_in_order():
    queued_late = question(question_id="late", fact_id="f-late", queue_key=NOW - timedelta(days=2))
    queued_early = question(question_id="early", fact_id="f-early", queue_key=NOW - timedelta(days=9))
    older = question(QuestionStatus.UNANSWERED, question_id="older", fact_id="f-older", asked_at=NOW - timedelta(days=5))
    newer = question(QuestionStatus.UNANSWERED, question_id="newer", fact_id="f-newer", asked_at=NOW - timedelta(days=2))
    current = question(QuestionStatus.ASKED, question_id="now", fact_id="f-now", asked_at=NOW)

    entries = queue_entries(
        state(current_question_id="now"), [queued_late, newer, queued_early, current, older], {}, "acc",
    )

    assert [(e.question_id, e.place) for e in entries] == [
        ("now", "current"), ("older", "sent"), ("newer", "sent"), ("early", "waiting"), ("late", "waiting"),
    ]


def test_without_a_state_no_question_is_current():
    asked = question(QuestionStatus.ASKED)

    [entry] = queue_entries(None, [asked], {}, "acc")

    assert entry.place == "sent"


def test_a_pending_question_that_is_not_the_current_one_waits():
    pending = question(QuestionStatus.PENDING, question_id="p")

    [entry] = queue_entries(FactReviewState(account_id="acc"), [pending], {}, "acc")

    assert (entry.place, entry.asked_at) == ("waiting", None)


def test_an_entry_shows_its_fact_as_it_stands_or_what_the_question_quoted():
    q = question(fact_id="f1", quoted_text="Drinks green tea")
    changed = fact("f1", text="Drinks black tea")

    [shown] = queue_entries(None, [q], {"f1": changed}, "acc")
    [gone] = queue_entries(None, [q], {}, "acc")

    assert (shown.text, shown.reported_date, shown.waiting_since) == ("Drinks black tea", changed.reported_date, q.queue_key)
    assert (gone.text, gone.domain, gone.reported_date) == ("Drinks green tea", None, None)

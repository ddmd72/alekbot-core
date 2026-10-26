"""The question module's public sets and predicate: open and sent statuses, the owner's sources, who it was asked of
(T12d, hexagonal review D-M1, D-L2, D-M3, S-M1)."""
from typing import get_args

import pytest

from src.domain.fact_lifecycle_day import CounterField
from src.domain.fact_review_question import (
    ANSWER_COUNTERS,
    OPEN_STATUSES,
    SENT_STATUSES,
    AnswerSource,
    QuestionStatus,
)
from tests.support.review_rule_builders import question


def test_every_status_but_closed_is_open():
    assert OPEN_STATUSES == frozenset(QuestionStatus) - {QuestionStatus.CLOSED}


def test_the_sent_statuses_are_the_ones_the_owner_was_shown():
    assert SENT_STATUSES == {QuestionStatus.ASKED, QuestionStatus.UNANSWERED}
    assert SENT_STATUSES <= OPEN_STATUSES


@pytest.mark.parametrize("status", list(QuestionStatus))
def test_is_open_reads_the_public_set(status):
    assert question(status).is_open is (status in OPEN_STATUSES)


def test_the_owners_sources_are_every_answer_source_but_the_review():
    assert set(ANSWER_COUNTERS) == set(get_args(AnswerSource)) - {"review"}


def test_each_owner_source_has_its_own_answer_counter():
    assert ANSWER_COUNTERS == {
        "digit": CounterField.ANSWERED_BY_DIGIT,
        "button": CounterField.ANSWERED_BY_BUTTON,
        "cabinet": CounterField.ANSWERED_IN_CABINET,
    }
    assert len(set(ANSWER_COUNTERS.values())) == len(ANSWER_COUNTERS)


def test_a_question_is_asked_of_its_user_in_its_account_only():
    asked = question(account_id="acc", user_id="u1")
    assert asked.asked_of("acc", "u1")
    assert not asked.asked_of("acc", "u2")
    assert not asked.asked_of("other", "u1")

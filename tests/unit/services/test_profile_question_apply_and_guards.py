"""What applying a profile answer comes to (`ProfileApply`) and the guards around it (fact profile RFC §4.7, PR 5d): an
option that is one of the question's is APPLIED (recorded, or its key already had an answer), one that is none and any
words are NOT_READ; a button on a delivered pending question completes it first; a button with no answers service wired
leaves the question open; the profile message refuses a lifecycle question. Invented cast."""
import logging

import pytest

from src.domain.fact_review_language import CANONICAL_LABELS
from src.domain.fact_review_question import QuestionStatus
from src.domain.profile_apply import ProfileApply
from src.domain.profile_question_message import profile_question_message
from src.services.profile_question_answers import ProfileQuestionAnswers
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import ReviewWorld
from tests.support.review_rule_builders import question as lifecycle_question
from tests.unit.services.test_profile_question_answers import Answers, ask, click, service
from tests.support.profile_question_builders import profile_question


def test_the_apply_outcomes_are_the_two_stored_strings_of_a_str_enum():
    assert {m.name: m.value for m in ProfileApply} == {"APPLIED": "applied", "NOT_READ": "not_read"}
    assert ProfileApply.APPLIED == "applied" and isinstance(ProfileApply.NOT_READ, str)
    assert ProfileApply("not_read") is ProfileApply.NOT_READ


async def test_an_option_of_the_question_is_applied_and_recorded_and_a_second_one_is_applied_but_not_recorded():
    store = Answers()
    answers = ProfileQuestionAnswers(store)
    question = profile_question("place")

    assert await answers.apply_option(question, "pick1", NOW) is ProfileApply.APPLIED
    assert await answers.apply_option(question, "pick2", NOW) is ProfileApply.APPLIED

    assert [a.geonameid for a in store.recorded] == ["1000"]


@pytest.mark.parametrize("kind", ["place", "person", "date"])
async def test_a_button_that_is_no_option_and_any_words_are_not_read_and_record_nothing(kind):
    store = Answers()
    answers = ProfileQuestionAnswers(store)
    question = profile_question(kind)

    assert await answers.apply_option(question, "pick9", NOW) is ProfileApply.NOT_READ
    assert await answers.apply_words(question, "Rome, Italy", NOW) is ProfileApply.NOT_READ

    assert store.recorded == []


async def test_a_button_on_a_delivered_pending_profile_question_completes_it_then_answers_it():
    world, store = ReviewWorld(), Answers()
    await ask(world, "place", QuestionStatus.PENDING)

    outcome = await click(world, service(world, store), "pick1", "pq-place")

    done = world.question_docs["pq-place"]
    assert outcome is not None and done["status"] == "closed" and done["asked_at"] is not None
    assert [a.geonameid for a in store.recorded] == ["1000"]


async def test_a_button_without_the_answers_service_leaves_the_question_open(caplog):
    world, store = ReviewWorld(), Answers()
    await ask(world, "place")

    with caplog.at_level(logging.WARNING):
        outcome = await click(world, service(world, store, wired=False), "pick1", "pq-place")

    assert outcome is None and world.question_docs["pq-place"]["status"] == "asked" and store.recorded == []
    assert any("no profile answers are wired" in r.getMessage() for r in caplog.records)


def test_the_profile_message_refuses_a_lifecycle_question():
    with pytest.raises(ValueError, match="only a profile question"):
        profile_question_message(lifecycle_question(), dict(CANONICAL_LABELS))

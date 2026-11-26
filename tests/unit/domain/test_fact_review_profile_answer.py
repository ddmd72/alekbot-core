"""The answer the review model gives to a profile question's own point (fact profile RFC §4.7): what code accepts
(`profile_answer_refusal`), the result's plain-data form with it, and what the pass's answers change after it
(`after_pass`). Invented cast."""
from datetime import timedelta

import pytest

from src.domain.entities import FactState
from src.domain.fact_lifecycle_day import CounterField
from src.domain.fact_review_after_pass import after_pass
from src.domain.fact_review_outcome import ReviewDecision
from src.domain.fact_review_profile_answer import ProfileAnswerRequest, profile_answer_refusal, shown_option_ids
from src.domain.fact_review_question import QuestionStatus
from src.domain.fact_review_result import FactReviewResult, decode_review_result, encode_review_result
from tests.support.profile_question_builders import NOW, profile_question
from tests.support.review_rule_builders import fact, question

WORDS = "It is me, Alex"
QUOTE = "me, Alex"


def replied(kind: str = "person", words: str = WORDS, **over):
    return profile_question(kind, QuestionStatus.UNANSWERED, **over).with_reply(words, NOW - timedelta(hours=1))


# ------------------------------------------------------------------------------------------------ the refusal

def test_a_shown_option_with_words_from_the_reply_is_accepted_for_every_kind():
    assert profile_answer_refusal(replied("person"), "pick1", QUOTE, NOW) is None
    assert profile_answer_refusal(replied("person"), "not_listed", QUOTE, NOW) is None
    assert profile_answer_refusal(replied("place", "Rome in Italy"), "pick1", "in italy", NOW) is None
    assert profile_answer_refusal(replied("place", "Rome in Italy"), "keep", "Rome", NOW) is None
    assert profile_answer_refusal(replied("date", "I do not know"), "dont_know", "do not know", NOW) is None


def test_the_quote_is_matched_as_the_review_matches_a_reply_quote_in_case_and_spacing():
    assert profile_answer_refusal(replied(), "pick1", "  ME,   ALEX ", NOW) is None


@pytest.mark.parametrize("answer", ["2026", "2026-10", "2026-10-01", "2026-10-01 to 2026-10-03", "2026-09 – 2026-10"])
def test_a_date_question_takes_a_date_of_one_precision_or_a_range_of_two(answer):
    assert profile_answer_refusal(replied("date", "It was in the autumn of 2026"), answer, "autumn of 2026", NOW) is None


def test_a_question_that_is_not_a_profile_question_is_refused():
    assert profile_answer_refusal(question(QuestionStatus.UNANSWERED, reply_text="yes", reply_at=NOW), "pick1", "yes", NOW).startswith(
        "Tool error:")


def test_a_closed_question_is_refused():
    closed = profile_question("person", QuestionStatus.CLOSED)
    assert "already closed" in profile_answer_refusal(closed, "pick1", "x", NOW)


def test_a_question_with_no_words_recorded_is_refused():
    open_one = profile_question("person", QuestionStatus.UNANSWERED)
    assert "no recorded words" in profile_answer_refusal(open_one, "pick1", "x", NOW)


@pytest.mark.parametrize("quote", ["", "   ", "Lukas Weber owns"])
def test_a_quote_that_is_not_in_the_reply_is_refused(quote):
    assert "quote is not in the owner's words" in profile_answer_refusal(replied(), "pick1", quote, NOW)


def test_an_answer_that_is_no_shown_option_is_refused_and_the_text_names_what_is_accepted():
    refusal = profile_answer_refusal(replied("person"), "pick3", QUOTE, NOW)

    assert refusal.startswith("Tool error:")
    assert all(option_id in refusal for option_id in ("pick1", "pick2", "not_listed", "dont_know"))
    assert "keep" not in refusal


def test_keep_is_a_place_option_and_is_refused_on_a_person_question():
    assert profile_answer_refusal(replied("person"), "keep", QUOTE, NOW).startswith("Tool error:")
    assert shown_option_ids(replied("person")) == ("pick1", "pick2", "not_listed", "dont_know")
    assert shown_option_ids(replied("place")) == ("pick1", "pick2", "keep", "dont_know")
    assert shown_option_ids(replied("date")) == ("dont_know",)


@pytest.mark.parametrize("answer", ["next friday", "2026-13", "2026 to 2026-10", "2026-10-01\n2026-10-03", "keep", "pick1"])
def test_a_date_that_is_no_accepted_form_is_refused_and_the_text_names_the_forms(answer):
    refusal = profile_answer_refusal(replied("date", "It was in the autumn of 2026"), answer, "autumn of 2026", NOW)

    assert refusal.startswith("Tool error:") and "YYYY-MM-DD" in refusal and "dont_know" in refusal


# ------------------------------------------------------------------------------------------------ the result

def test_the_result_carries_its_profile_answers_through_the_plain_data_form():
    result = FactReviewResult(
        outcomes=(ReviewDecision("f1", "revived", "f1", "evidence"),),
        profile_answers=(ProfileAnswerRequest("pq-person", "pick1", QUOTE), ProfileAnswerRequest("pq-date", "2026-10", "x")),
    )

    encoded = encode_review_result(result)

    assert encoded["profile_answers"] == [
        {"question_id": "pq-person", "answer": "pick1", "quote": QUOTE},
        {"question_id": "pq-date", "answer": "2026-10", "quote": "x"},
    ]
    assert decode_review_result(encoded) == result
    assert decode_review_result({"kept": ["f1"]}).profile_answers == ()


# ------------------------------------------------------------------------------------------------ after the pass

def test_an_answered_profile_question_is_not_closed_by_the_pass_and_is_counted_with_the_replies():
    batch = [fact("f-person", state=FactState.CURRENT), fact("f-date", state=FactState.CURRENT)]
    person = replied("person", fact_id="f-person")
    date = replied("date", "It was in the autumn of 2026", fact_id="f-date")
    result = FactReviewResult(
        outcomes=(ReviewDecision("f-person", "rewritten", "f-person-2", "reply"),),
        profile_answers=(ProfileAnswerRequest("pq-person", "pick1", QUOTE),),
    )

    plan = after_pass(batch, [person, date], result, NOW)

    assert plan.answered == ("pq-person",)
    assert plan.closes == ()                            # decided, yet it closes on the record of its answer
    assert plan.counts[CounterField.CLOSED_BY_REPLY] == 1


def test_an_answer_for_a_question_outside_the_batch_is_ignored():
    batch = [fact("f-person", state=FactState.CURRENT)]
    elsewhere = replied("person", question_id="pq-else", fact_id="f-other")

    plan = after_pass(batch, [elsewhere], FactReviewResult(profile_answers=(ProfileAnswerRequest("pq-else", "pick1", QUOTE),)), NOW)

    assert plan.answered == () and plan.closes == ()


def test_a_decided_fact_still_closes_its_lifecycle_question_when_only_the_profile_one_is_answered():
    batch = [fact("f1", state=FactState.CURRENT)]
    lifecycle = question(QuestionStatus.ASKED, question_id="q1", fact_id="f1")
    person = replied("person", fact_id="f1")
    result = FactReviewResult(
        outcomes=(ReviewDecision("f1", "revived", "f1", "reply"),),
        profile_answers=(ProfileAnswerRequest("pq-person", "pick1", QUOTE),),
    )

    plan = after_pass(batch, [lifecycle, person], result, NOW)

    assert [q.question_id for q in plan.closes] == ["q1"] and plan.answered == ("pq-person",)

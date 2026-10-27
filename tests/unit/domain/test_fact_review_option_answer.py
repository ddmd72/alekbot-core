"""`option_answer` (Facts S4 · T8g): the answer a button of the question's message stands for."""
import pytest

from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_answers import digit_answer, option_answer
from src.domain.fact_review_language import LABEL_KEYS, fact_question_message
from src.domain.fact_review_options import OPTIONS
from tests.support.review_rule_builders import question
from tests.unit.domain.review_p2_texts import TEXTS


@pytest.mark.parametrize("kind", list(AnswerKind))
def test_every_option_of_the_message_is_the_answer_of_the_same_name(kind):
    # P2 revised (C11): a message offers the options of its question's kind; each answer is asked on a kind it belongs to
    qkind = next(k for k, options in OPTIONS.items() if kind in options)
    asked = question() if qkind == "still_true" else question(question=qkind, fallback="history", texts=TEXTS[qkind])
    message = fact_question_message(asked, {key: key for key in LABEL_KEYS})

    assert option_answer(kind.value) is kind
    assert message.option(kind.value) is not None


def test_a_button_and_its_digit_are_the_same_answer():
    message = fact_question_message(question(), {key: key for key in LABEL_KEYS})

    for number, option in message.numbered_options():
        assert option_answer(option.id) is digit_answer(message, str(number))


@pytest.mark.parametrize("option_id", ["", "maybe", "STILL_TRUE", "still true", "1"])
def test_an_id_the_message_does_not_offer_is_no_answer(option_id):
    assert option_answer(option_id) is None

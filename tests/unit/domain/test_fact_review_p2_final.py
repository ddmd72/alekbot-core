"""Facts P2 · final review: an ask whose texts repeat the stored text, the history fallback's give-up rule, and the
single sources of the history answers, the payment keys and the question kinds."""
from datetime import timedelta
from typing import get_args

import pytest

from src.domain import fact_review_answers, fact_review_reasons, fact_review_rules
from src.domain.entities import ExpiryKind
from src.domain.fact_review_answer_kind import HISTORY_ANSWERS, AnswerKind
from src.domain.fact_review_basis import PAYMENT_KEYS, parse_payment
from src.domain.fact_review_question import TEXT_KEYS, FactReviewQuestion, QuestionKind
from src.domain.fact_review_rules import MAX_FALLBACK_ATTEMPTS, ask_refusal, history_fallback_gives_up
from tests.support.review_rule_builders import NOW, fact

TALK = "Alex gives a talk at the Hamburg conference"
TEXTS = {"happened": "Gave the talk at the Hamburg conference", "not_happened": "Did not give the talk",
         "unknown": "Planned the talk; unknown whether it took place"}


def asked(texts, text=TALK):
    subject = fact("f1", text=text, expiry_kind=ExpiryKind.DATE, expires_at=NOW - timedelta(days=3))
    return ask_refusal(subject, "history", None, False, None, NOW, "UTC", question="happened", texts=texts)


def test_distinct_texts_are_asked():
    assert asked(TEXTS) is None


@pytest.mark.parametrize("key", sorted(TEXTS))
def test_a_text_equal_to_the_stored_text_is_refused_whatever_its_spacing_and_case(key):
    refusal = asked({**TEXTS, key: "  alex gives a TALK at the  Hamburg conference "})

    assert refusal == f"ask_owner.texts.{key}: the same as the fact's stored text; write what the answer makes true"


@pytest.mark.parametrize("attempts,refused,gives_up", [
    (0, False, False), (0, True, False),       # the first failure, of either kind, leaves the question open
    (1, False, False), (1, True, True),        # a typed refusal after one failed attempt gives it up
    (2, False, True), (2, True, True),         # the third failure of any kind gives it up
])
def test_the_history_fallback_give_up_rule(attempts, refused, gives_up):
    question = FactReviewQuestion(
        question_id="q1", account_id="acc", user_id="u1", fact_id="f1", quoted_text=TALK, fallback="history",
        queue_key=NOW, queued_at=NOW, question="happened", texts=TEXTS, fallback_attempts=attempts,
    )

    assert history_fallback_gives_up(question, refused) is gives_up
    assert MAX_FALLBACK_ATTEMPTS == 3


def test_the_history_answers_have_one_definition():
    assert HISTORY_ANSWERS == {
        AnswerKind.HAPPENED, AnswerKind.NOT_HAPPENED, AnswerKind.NOT_NEEDED, AnswerKind.PAID, AnswerKind.NOT_PAID,
    }
    assert not hasattr(fact_review_answers, "_HISTORY_ANSWERS") and not hasattr(fact_review_reasons, "_HISTORY_ANSWERS")


def test_the_payment_keys_are_the_digest_paid_texts():
    assert PAYMENT_KEYS == TEXT_KEYS[("paid", "digest")] == ("paid", "not_paid")
    assert parse_payment({"not_paid": "b", "paid": "a"}) == {"paid": "a", "not_paid": "b"}
    with pytest.raises(ValueError, match=r"payment must be \{paid: <text>, not_paid: <text>\}"):
        parse_payment({"paid": "a"})
    assert not hasattr(fact_review_rules, "_PAYMENT_KEYS")


def test_the_question_kinds_are_the_literals():
    assert fact_review_rules._QUESTION_KINDS == get_args(QuestionKind)

"""P2-3: the question record's kind, texts, origin, delivery and fallback attempts, and the options of each kind."""
from datetime import date

import pytest

from src.domain.exceptions import QuestionTransitionError
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_options import OPTIONS, options_of, text_key
from src.domain.fact_review_question import (
    DIGEST_QUESTION_DAYS, TEXT_KEYS, UNKNOWN_TEXT, ClosedBy, FactReviewQuestion, QuestionStatus,
)
from tests.unit.domain.review_p2_texts import DIGEST_TEXTS, HAPPENED_TEXTS, PAID_TEXTS, RENEWED_TEXTS, TEXTS
from tests.support.review_rule_builders import NOW, question

S = QuestionStatus


def test_the_constants_are_the_contracts():
    assert UNKNOWN_TEXT == "unknown"
    assert DIGEST_QUESTION_DAYS.days == 90
    assert dict(TEXT_KEYS) == {
        ("still_true", "chat"): (),
        ("happened", "chat"): ("happened", "not_happened", "unknown"),
        ("renewed", "chat"): ("renewed", "not_renewed", "not_needed", "unknown"),
        ("paid", "chat"): ("paid", "not_paid", "unknown"),
        ("paid", "digest"): ("paid", "not_paid"),
    }


def test_an_s4_record_with_none_of_the_new_keys_still_validates_with_their_defaults():
    stored = question(S.ASKED).model_dump()
    for key in ("question", "texts", "origin", "delivery", "fallback_attempts"):
        stored.pop(key)
    restored = FactReviewQuestion.model_validate(stored)
    assert (restored.question, restored.texts, restored.origin, restored.delivery, restored.fallback_attempts) == (
        "still_true", {}, "review", "chat", 0,
    )
    s4_archive = question(fallback="archive", fallback_date=date(2026, 10, 1)).model_dump()
    for key in ("question", "texts", "origin", "delivery", "fallback_attempts"):
        s4_archive.pop(key)
    assert FactReviewQuestion.model_validate(s4_archive).fallback_date == date(2026, 10, 1)


# ------------------------------------------------------------------------------------------------ kind x origin x delivery

ACCEPTED = [
    dict(question="still_true", fallback="revive"),
    dict(question="still_true", fallback="archive", noise=True),
    dict(question="still_true", fallback="archive"),  # an S4 archive question
    dict(question="happened", fallback="history", texts=HAPPENED_TEXTS),
    dict(question="happened", fallback="archive", noise=True, texts=HAPPENED_TEXTS),
    dict(question="renewed", fallback="history", texts=RENEWED_TEXTS),
    dict(question="renewed", fallback="archive", noise=True, texts=RENEWED_TEXTS),
    dict(question="paid", fallback="history", texts=PAID_TEXTS),
    dict(question="paid", fallback="history", texts=DIGEST_TEXTS, delivery="digest"),
    dict(question="still_true", fallback="revive", origin="kept_twice"),
    dict(question="happened", fallback="history", origin="kept_twice"),
]


@pytest.mark.parametrize("fields", ACCEPTED, ids=lambda f: "-".join(str(v) for v in f.values() if not isinstance(v, dict)))
def test_each_legal_combination_validates(fields):
    built = question(**fields)
    assert built.question == fields["question"]


REFUSED = {
    "a digest still_true": dict(question="still_true", fallback="revive", delivery="digest"),
    "a digest happened": dict(question="happened", fallback="history", texts=HAPPENED_TEXTS, delivery="digest"),
    "a digest kept twice": dict(question="paid", fallback="history", origin="kept_twice", delivery="digest"),
    "a happened without texts": dict(question="happened", fallback="history"),
    "a happened missing unknown": dict(question="happened", fallback="history", texts={
        k: v for k, v in HAPPENED_TEXTS.items() if k != "unknown"}),
    "a happened with an extra key": dict(question="happened", fallback="history", texts={**HAPPENED_TEXTS, "paid": "x"}),
    "a blank text": dict(question="happened", fallback="history", texts={**HAPPENED_TEXTS, "happened": "  "}),
    "a still_true with texts": dict(question="still_true", fallback="revive", texts={"unknown": "x"}),
    "a chat paid with digest texts": dict(question="paid", fallback="history", texts=DIGEST_TEXTS),
    "a digest paid with chat texts": dict(question="paid", fallback="history", texts=PAID_TEXTS, delivery="digest"),
    "a kept twice with texts": dict(question="happened", fallback="history", origin="kept_twice", texts=HAPPENED_TEXTS),
    "a kept twice renewed": dict(question="renewed", fallback="history", origin="kept_twice"),
    "a kept twice paid": dict(question="paid", fallback="history", origin="kept_twice"),
    "a still_true falling back to history": dict(question="still_true", fallback="history"),
    "a happened reviving": dict(question="happened", fallback="revive", texts=HAPPENED_TEXTS),
    "a happened archiving without noise": dict(question="happened", fallback="archive", texts=HAPPENED_TEXTS),
    "a renewed reviving": dict(question="renewed", fallback="revive", texts=RENEWED_TEXTS),
    "a paid reviving": dict(question="paid", fallback="revive", texts=PAID_TEXTS),
    "a paid archived as noise": dict(question="paid", fallback="archive", noise=True, texts=PAID_TEXTS),
    "an unknown kind": dict(question="moved", fallback="revive"),
    "an unknown origin": dict(question="still_true", fallback="revive", origin="janitor"),
    "an unknown delivery": dict(question="still_true", fallback="revive", delivery="email"),
    "a negative attempt count": dict(question="still_true", fallback="revive", fallback_attempts=-1),
    "noise on a history fallback": dict(question="happened", fallback="history", noise=True, texts=HAPPENED_TEXTS),
}


@pytest.mark.parametrize("name", REFUSED)
def test_each_illegal_combination_is_refused(name):
    with pytest.raises(ValueError):
        question(**REFUSED[name])


def test_a_question_with_texts_is_not_hashable():
    with pytest.raises(TypeError):
        hash(question(question="happened", fallback="history", texts=HAPPENED_TEXTS))


# ------------------------------------------------------------------------------------------------ reads and moves

def test_in_chat_is_the_delivery():
    assert question().in_chat
    assert not question(question="paid", fallback="history", texts=DIGEST_TEXTS, delivery="digest").in_chat


@pytest.mark.parametrize("status", [S.QUEUED, S.PENDING, S.ASKED, S.UNANSWERED])
def test_a_failed_fallback_counts_one_more_attempt_and_changes_nothing_else(status):
    before = question(status, question="happened", fallback="history", texts=HAPPENED_TEXTS)
    once = before.fallback_failed()
    twice = once.fallback_failed()
    assert (once.fallback_attempts, twice.fallback_attempts) == (1, 2)
    assert once.model_dump(exclude={"fallback_attempts"}) == before.model_dump(exclude={"fallback_attempts"})
    assert before.fallback_attempts == 0  # frozen: a new question each time


def test_a_closed_question_cannot_fail_its_fallback():
    closed = question(S.ASKED).closed(ClosedBy.TIMEOUT, NOW)
    with pytest.raises(QuestionTransitionError):
        closed.fallback_failed()


def test_the_new_fields_survive_every_move():
    asked = question(S.ASKED, question="renewed", fallback="history", texts=RENEWED_TEXTS)
    closed = asked.unanswered().fallback_failed().closed(ClosedBy.OWNER, NOW, AnswerKind.RENEWED, "digit")
    assert (closed.question, closed.texts, closed.fallback_attempts) == ("renewed", RENEWED_TEXTS, 1)


# ------------------------------------------------------------------------------------------------ the options

def test_the_options_of_each_kind_in_digit_order():
    A = AnswerKind
    assert dict(OPTIONS) == {
        "still_true": (A.STILL_TRUE, A.ENDED, A.NEVER, A.DONT_KNOW),
        "happened": (A.HAPPENED, A.NOT_HAPPENED, A.DONT_KNOW),
        "renewed": (A.RENEWED, A.NOT_RENEWED, A.NOT_NEEDED, A.DONT_KNOW),
        "paid": (A.PAID, A.NOT_PAID, A.DONT_KNOW),
    }


@pytest.mark.parametrize("kind", ["still_true", "happened", "renewed", "paid"])
def test_a_chat_question_offers_its_kinds_options(kind):
    fallback = "revive" if kind == "still_true" else "history"
    assert options_of(question(question=kind, fallback=fallback, texts=TEXTS[kind])) == OPTIONS[kind]


def test_a_digest_question_offers_no_dont_know():
    digest = question(question="paid", fallback="history", texts=DIGEST_TEXTS, delivery="digest")
    assert options_of(digest) == (AnswerKind.PAID, AnswerKind.NOT_PAID)


def test_the_new_answer_kinds_follow_the_s4_ones_in_the_contracts_order():
    assert [k.value for k in AnswerKind] == [
        "still_true", "ended", "never", "dont_know", "happened", "not_happened", "renewed", "not_renewed",
        "not_needed", "paid", "not_paid",
    ]


@pytest.mark.parametrize("kind", list(AnswerKind))
def test_an_answers_text_key_is_its_value_and_dont_know_writes_the_unknown_text(kind):
    assert text_key(kind) == ("unknown" if kind is AnswerKind.DONT_KNOW else kind.value)

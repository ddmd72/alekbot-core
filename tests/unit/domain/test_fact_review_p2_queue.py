"""P2-2, P2-3: the batch order that keeps the parts of one plan together, the questions code builds (kept twice, a
routine invoice's digest question), and a digest question kept out of the chat queue, the pool and the counts."""
from datetime import timedelta

import pytest

from src.domain.entities import ExpiryKind, FactState
from src.domain.fact_lifecycle_review_line import question_standing
from src.domain.fact_review_question import QuestionStatus
from src.domain.fact_review_rules import (
    QUEUE_ALERT, digest_question, digest_question_expired, kept_twice_fallback, kept_twice_question, next_in_queue,
    open_fact_ids, pool, queue_alert, queue_key, queued_question, review_due, select_batch,
)
from tests.support.review_rule_builders import NOW, fact, question, state
from tests.unit.domain.review_p2_texts import DIGEST_TEXTS, HAPPENED_TEXTS

S = QuestionStatus
STALED = NOW - timedelta(days=3)
INVOICE = "An invoice for the phone bill (45 EUR), due Nov 3, 2026, was received; payment not confirmed."


def digest_q(question_id="qd", fact_id="fd", **over):
    return question(
        question_id=question_id, fact_id=fact_id, question="paid", fallback="history", texts=DIGEST_TEXTS,
        delivery="digest", **over,
    )


# ------------------------------------------------------------------------------------------------ the batch order

def test_equal_offers_and_staleness_go_by_expires_at_with_none_last():
    hotel = fact("a-hotel", stale_since=STALED, expires_at=NOW - timedelta(days=4))
    flight = fact("b-flight", stale_since=STALED, expires_at=NOW - timedelta(days=5))
    open_ended = fact("0-note", stale_since=STALED, expiry_kind=ExpiryKind.NONE, ttl_days=None, expires_at=None)
    transfer = fact("c-transfer", stale_since=STALED, expires_at=NOW - timedelta(days=4))
    batch = select_batch([open_ended, hotel, transfer, flight], [], [], [], {})
    assert [f.id for f in batch] == ["b-flight", "a-hotel", "c-transfer", "0-note"]


def test_offers_and_staleness_still_come_first():
    early_ending = fact("x", stale_since=STALED, expires_at=NOW - timedelta(days=60))
    longer_stale = fact("y", stale_since=STALED - timedelta(days=1), expires_at=NOW - timedelta(days=1))
    offered_once = fact("z", stale_since=STALED - timedelta(days=9), expires_at=NOW - timedelta(days=90))
    batch = select_batch([early_ending, longer_stale, offered_once], [], [], [], {"z": 1})
    assert [f.id for f in batch] == ["y", "x", "z"]


# ------------------------------------------------------------------------------------------------ questions code builds

def test_a_date_fact_kept_twice_is_asked_happened_with_the_history_fallback():
    dated = fact("f1", expiry_kind=ExpiryKind.DATE, ttl_days=None)
    built = kept_twice_question(dated, "q9", "u1", at=NOW)
    assert kept_twice_fallback(dated) == "history"
    assert (built.question, built.origin, built.fallback, built.texts, built.delivery) == (
        "happened", "kept_twice", "history", {}, "chat",
    )
    assert (built.question_id, built.fact_id, built.quoted_text, built.queue_key, built.queued_at) == (
        "q9", "f1", dated.text, queue_key(dated), NOW,
    )


def test_any_other_fact_kept_twice_is_asked_still_true_with_revive():
    built = kept_twice_question(fact("f1"), "q9", "u1", at=NOW)
    assert kept_twice_fallback(fact("f1")) == "revive"
    assert (built.question, built.origin, built.fallback, built.texts) == ("still_true", "kept_twice", "revive", {})


def test_queued_question_carries_the_asks_kind_texts_and_delivery():
    built = queued_question(fact("f1"), "q9", "u1", "history", at=NOW, question="happened", texts=HAPPENED_TEXTS)
    assert (built.question, built.texts, built.origin, built.delivery) == ("happened", HAPPENED_TEXTS, "review", "chat")
    s4 = queued_question(fact("f1"), "q9", "u1", "revive", at=NOW)
    assert (s4.question, s4.texts, s4.origin, s4.delivery) == ("still_true", {}, "review", "chat")


def test_a_routine_invoice_gets_a_digest_question_on_its_history_version():
    written = fact("f1-v2", state=FactState.CURRENT, text=INVOICE, stale_since=None, expiry_kind=ExpiryKind.NONE,
                   ttl_days=None, expires_at=None)
    built = digest_question(written, "q9", "u1", DIGEST_TEXTS["paid"], DIGEST_TEXTS["not_paid"], at=NOW)
    assert (built.question, built.delivery, built.origin, built.fallback) == ("paid", "digest", "review", "history")
    assert built.texts == DIGEST_TEXTS and built.quoted_text == INVOICE and built.fact_id == "f1-v2"
    assert (built.queue_key, built.queued_at, built.status) == (NOW, NOW, S.QUEUED)
    assert not built.in_chat


def test_a_digest_question_expires_ninety_days_after_it_was_queued():
    queued = digest_q(queued_at=NOW - timedelta(days=90))
    assert digest_question_expired(queued, NOW)
    assert not digest_question_expired(queued, NOW - timedelta(seconds=1))
    assert not digest_question_expired(question(queued_at=NOW - timedelta(days=200)), NOW)  # a chat question never


# ------------------------------------------------------------------------------------------------ out of the chat queue

def test_a_digest_question_holds_no_fact_and_is_not_in_the_pool():
    questions = [question(fact_id="f1"), digest_q(fact_id="fd")]
    assert open_fact_ids(questions) == {"f1"}
    assert pool(3, questions) == 4


def test_a_digest_question_is_never_next_and_the_chat_one_is():
    older_digest = digest_q(queue_key=NOW - timedelta(days=400))
    chat = question(question_id="qc")
    assert next_in_queue([older_digest]) is None
    assert next_in_queue([older_digest, chat]).question_id == "qc"


def test_digest_questions_do_not_raise_the_queue_alert():
    many = [digest_q(question_id=f"d{i}", fact_id=f"fd{i}") for i in range(QUEUE_ALERT + 5)]
    assert queue_alert(state(), many) is None
    chats = [question(question_id=f"c{i}", fact_id=f"fc{i}") for i in range(QUEUE_ALERT + 1)]
    assert queue_alert(state(), chats) == "raise"


def test_a_queued_digest_question_does_not_make_a_review_due():
    assert review_due(0, state(last_review_at=NOW), [digest_q()], 1, NOW, "UTC") is None
    assert review_due(0, state(last_review_at=NOW), [question()], 1, NOW, "UTC") == "queue"


def test_a_digest_question_is_not_in_the_standing():
    standing = question_standing([digest_q(), question(question_id="qc")])
    assert (standing.queued, standing.unanswered) == (1, 0)


@pytest.mark.parametrize("held", [True, False])
def test_a_stale_fact_under_a_digest_question_is_still_batched(held):
    stale = fact("fd", stale_since=STALED)
    questions = [digest_q(fact_id="fd")] if held else []
    assert [f.id for f in select_batch([stale], questions, [], [], {})] == ["fd"]

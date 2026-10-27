"""Facts P2 · card B: the owner's answers to the new question kinds, on the review world (the real repository, typed write
path, review store and counters over one evaluating Firestore fake). A digit, a button and a Cabinet click each reach the
write of their kind; an answer that is no option of its question writes nothing."""
from datetime import timedelta
from typing import Any, Dict, List, Optional

import pytest

from src.domain.entities import ExpiryKind
from src.domain.fact_answer_result import AnswerResult
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_language import fact_question_message
from src.domain.fact_review_question import ClosedBy, FactReviewQuestion, QuestionStatus
from src.domain.language import LanguageCode
from tests.support.fact_kit import ACC, USER
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import ReviewWorld
from tests.unit.services.test_fact_answer_service import day_row, service, texts_of
from tests.unit.services.test_fact_question_choice import choice_of

POTTERY = "Alex has a pottery class on Saturday"
GYM = "Alex has a gym membership"
PHONE = "The phone bill of October arrived"
HAPPENED = {"happened": "Went to the pottery class", "not_happened": "Skipped the pottery class",
            "unknown": "Planned a pottery class; unknown whether it took place"}
RENEWED = {"renewed": "Renewed the gym membership", "not_renewed": "Let the gym membership lapse",
           "not_needed": "No longer needs a gym membership", "unknown": "Gym membership renewal unknown"}
PAID = {"paid": "The October phone bill is paid", "not_paid": "The October phone bill is not paid",
        "unknown": "The October phone bill: payment unknown"}


async def fact_for(world: ReviewWorld, text: str, kind: str) -> str:
    if kind == "happened":
        return await world.seed(text, expiry_kind=ExpiryKind.DATE, expires_at=NOW - timedelta(days=2),
                                reported_date=NOW - timedelta(days=30))
    return await world.seed(text, expiry_kind=ExpiryKind.HORIZON, ttl_days=30, reported_date=NOW - timedelta(days=20),
                            expires_at=NOW + timedelta(days=10))


async def asked_question(world: ReviewWorld, kind: str, texts: Dict[str, str], delivery: str = "chat",
                         text: Optional[str] = None) -> FactReviewQuestion:
    subject = text or {"happened": POTTERY, "renewed": GYM, "paid": PHONE}[kind]
    fact_id = await fact_for(world, subject, kind)
    fields: Dict[str, Any] = dict(
        question_id="q1", account_id=ACC, user_id=USER, fact_id=fact_id, quoted_text=subject, question=kind,
        texts=texts, fallback="history", delivery=delivery, queue_key=NOW, queued_at=NOW)
    if delivery == "digest":
        return await world.seed_question(QuestionStatus.QUEUED, **fields)
    draft = FactReviewQuestion(**fields)
    labels = await texts_of(world).labels_for(USER, ACC)
    fields["message_ref"] = await world.interactive.post("slack", "D1", fact_question_message(draft, labels))
    return await world.seed_question(QuestionStatus.ASKED, **{k: v for k, v in fields.items() if k not in ("queue_key", "queued_at")})


def versions(world: ReviewWorld, fact_id: str) -> List[dict]:
    lineage = world.docs[fact_id]["lineage_id"]
    return sorted((d for d in world.docs.values() if d.get("lineage_id") == lineage), key=lambda d: d["version"])


# (question kind, texts, answer, source) -> (written text, trail reason, expiry kind written)
WRITES = [
    ("happened", HAPPENED, AnswerKind.HAPPENED, "digit",
     "Went to the pottery class", "rewritten as history q=q1: happened (digit)", "none"),
    ("happened", HAPPENED, AnswerKind.NOT_HAPPENED, "button",
     "Skipped the pottery class", "rewritten as history q=q1: not_happened (button)", "none"),
    ("happened", HAPPENED, AnswerKind.DONT_KNOW, "cabinet",
     "Planned a pottery class; unknown whether it took place", "rewritten as history: did not remember q=q1", "none"),
    ("renewed", RENEWED, AnswerKind.RENEWED, "digit",
     "Renewed the gym membership", "renewed q=q1 (digit)", "horizon"),
    ("renewed", RENEWED, AnswerKind.NOT_RENEWED, "button",
     "Let the gym membership lapse", "rewritten q=q1: not_renewed (button)", "none"),
    ("renewed", RENEWED, AnswerKind.NOT_NEEDED, "cabinet",
     "No longer needs a gym membership", "rewritten as history q=q1: not_needed (cabinet)", "none"),
    ("paid", PAID, AnswerKind.PAID, "digit",
     "The October phone bill is paid", "rewritten as history q=q1: paid (digit)", "none"),
    ("paid", PAID, AnswerKind.NOT_PAID, "button",
     "The October phone bill is not paid", "rewritten as history q=q1: not_paid (button)", "none"),
]


@pytest.mark.parametrize("kind,texts,answer,source,text,reason,expiry", WRITES,
                         ids=[f"{w[0]}-{w[2].value}" for w in WRITES])
async def test_each_answer_of_each_kind_reaches_its_write_and_closes_the_question(
        kind, texts, answer, source, text, reason, expiry):
    world = ReviewWorld()
    question = await asked_question(world, kind, texts)

    result = await service(world).apply(ACC, USER, "q1", answer, source, world.now)

    assert result is AnswerResult.APPLIED
    *_, new = versions(world, question.fact_id)
    assert new["text"] == text and new["state"] == "current" and new["expiry_kind"] == expiry
    assert new["lifecycle_events"][-1]["reason"] == reason
    assert new["lifecycle_events"][-1]["actor"] == "owner"
    closed = world.question_docs["q1"]
    assert (closed["status"], closed["closed_by"], closed["answer"], closed["answer_source"]) == (
        "closed", ClosedBy.OWNER.value, answer.value, source)


async def test_a_digit_reply_to_a_happened_question_means_the_options_of_that_kind():
    world = ReviewWorld()
    question = await asked_question(world, "happened", HAPPENED)

    reply = await service(world).answer_reply(
        USER, ACC, "slack", question.message_ref.channel_id, question.message_ref.message_id, "2", world.now)

    assert reply is not None
    *_, new = versions(world, question.fact_id)
    assert new["text"] == HAPPENED["not_happened"]                             # digit 2 of happened is "it did not"
    assert world.question_docs["q1"]["answer"] == "not_happened"


async def test_a_button_click_on_a_renewed_question_applies_its_option():
    world = ReviewWorld()
    question = await asked_question(world, "renewed", RENEWED)

    outcome = await service(world).handle(choice_of("renewed", question.message_ref, "q1"), USER, ACC)

    assert outcome is not None
    *_, new = versions(world, question.fact_id)
    assert new["text"] == RENEWED["renewed"] and new["expiry_kind"] == "horizon"


@pytest.mark.parametrize("kind,texts,answer", [
    ("still_true", {}, AnswerKind.HAPPENED),
    ("happened", HAPPENED, AnswerKind.STILL_TRUE),
    ("happened", HAPPENED, AnswerKind.PAID),
    ("renewed", RENEWED, AnswerKind.NOT_PAID),
])
async def test_an_answer_that_is_no_option_of_the_question_writes_nothing(kind, texts, answer):
    world = ReviewWorld()
    if kind == "still_true":
        fact_id = await world.seed(GYM, expiry_kind=ExpiryKind.HORIZON, ttl_days=30, reported_date=NOW - timedelta(days=20),
                                   expires_at=NOW + timedelta(days=10))
        await world.seed_question(QuestionStatus.QUEUED, question_id="q1", account_id=ACC, user_id=USER,
                                  fact_id=fact_id, quoted_text=GYM)
    else:
        fact_id = (await asked_question(world, kind, texts)).fact_id
    before = dict(world.docs[fact_id])

    result = await service(world).apply(ACC, USER, "q1", answer, "cabinet", world.now)

    assert result is AnswerResult.NOT_APPLIED
    assert world.docs[fact_id] == before and len(versions(world, fact_id)) == 1
    assert world.question_docs["q1"]["status"] != "closed"
    assert (await day_row(world)).answers_not_applied == 1


async def test_dont_know_on_a_digest_question_writes_nothing():
    world = ReviewWorld()
    question = await asked_question(world, "paid", {"paid": PAID["paid"], "not_paid": PAID["not_paid"]},
                                    delivery="digest")

    result = await service(world).apply(ACC, USER, "q1", AnswerKind.DONT_KNOW, "cabinet", world.now)

    assert result is AnswerResult.NOT_APPLIED
    assert len(versions(world, question.fact_id)) == 1 and world.question_docs["q1"]["status"] == "queued"


async def test_a_digest_question_is_answered_paid_in_the_cabinet():
    world = ReviewWorld()
    question = await asked_question(world, "paid", {"paid": PAID["paid"], "not_paid": PAID["not_paid"]},
                                    delivery="digest")

    result = await service(world).apply(ACC, USER, "q1", AnswerKind.PAID, "cabinet", world.now)

    assert result is AnswerResult.APPLIED
    *_, new = versions(world, question.fact_id)
    assert new["text"] == PAID["paid"]


async def test_the_answer_options_per_kind_are_the_deciding_answers_in_the_users_language():
    world = ReviewWorld()
    world.language.preferences[USER] = (LanguageCode.UK, False)

    options = await service(world).answer_options(USER, ["happened", "renewed", "paid", "still_true", "paid"])

    assert list(options) == ["happened", "renewed", "paid", "still_true"]
    assert [a for a, _ in options["happened"]] == [AnswerKind.HAPPENED, AnswerKind.NOT_HAPPENED]
    assert [a for a, _ in options["renewed"]] == [AnswerKind.RENEWED, AnswerKind.NOT_RENEWED, AnswerKind.NOT_NEEDED]
    assert [a for a, _ in options["paid"]] == [AnswerKind.PAID, AnswerKind.NOT_PAID]
    assert [a for a, _ in options["still_true"]] == [AnswerKind.STILL_TRUE, AnswerKind.ENDED, AnswerKind.NEVER]
    labels = await texts_of(world).ui_labels(USER)
    assert [label for _, label in options["happened"]] == [
        labels["memory_option_happened"], labels["memory_option_not_happened"]]
    assert all(label != "Paid" for _, label in options["paid"])

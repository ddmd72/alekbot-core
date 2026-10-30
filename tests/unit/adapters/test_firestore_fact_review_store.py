"""Facts S4 · T4a1: the review store's question records and replies (plan §2.2, §3.13), the real adapter on the review
world's evaluating Firestore fake with the declared indexes enforced. Assertions are on what is stored and what a reader
returns; every status change is the question's own move."""
import logging
from datetime import date, timedelta

import pytest
from google.api_core.exceptions import FailedPrecondition
from pydantic import ValidationError

from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_reply_record import ReplyRecord
from src.domain.fact_review_question import (
    MAX_REPLY_CHARS, QUESTION_RETENTION, ClosedBy, FactReviewQuestion, QuestionStatus,
)
from src.domain.messaging import MessageRef
from src.ports.fact_review_store import FactReviewStore
from tests.support.fact_review_world import ReviewWorld
from tests.support.review_rule_builders import NOW, REF
from tests.support.review_rule_builders import question as build_question

AT = NOW + timedelta(hours=1)


@pytest.fixture
def world():
    return ReviewWorld()


def stored(world, question_id):
    return world.question_docs[question_id]


def model_fields():
    return set(FactReviewQuestion.model_fields) - {"message_ref"} | {"message_platform", "message_channel", "message_id"}


async def a_fully_filled_question(world) -> FactReviewQuestion:
    """Every field of the model set: an archive fallback with a date and noise, sent, replied to, shown to the review,
    closed by the owner with a digit."""
    asked = build_question(
        QuestionStatus.ASKED, question_id="full", fallback="archive", fallback_date=date(2026, 12, 1), noise=True)
    full = (asked.with_reply("it ended in spring", NOW).offered(NOW + timedelta(minutes=5))
            .closed(ClosedBy.OWNER, AT, AnswerKind.ENDED, "digit"))
    return await world.put_question(full)


# ------------------------------------------------------------------------------------------------ the stored form

def test_the_adapter_is_the_review_store_port(world):
    assert isinstance(world.review_store, FactReviewStore)


async def test_every_field_round_trips_through_the_stored_form(world):
    full = await a_fully_filled_question(world)
    assert all(getattr(full, name) is not None for name in FactReviewQuestion.model_fields)

    [back] = await world.review_store.get_questions(["full"])

    assert back == full
    doc = stored(world, "full")
    assert set(doc) == model_fields() | {"purge_after"}          # the store's TTL field, on a closed question only
    assert (doc["message_platform"], doc["message_channel"], doc["message_id"]) == (REF.platform, REF.channel_id, REF.message_id)
    assert doc["status"] == "closed" and type(doc["status"]) is str
    assert doc["closed_by"] == "owner" and doc["answer"] == "ended" and doc["answer_source"] == "digit"
    assert doc["fallback_date"] == "2026-12-01"
    assert doc["queue_key"].tzinfo is not None and doc["purge_after"] == AT + QUESTION_RETENTION


async def test_a_queued_question_round_trips_with_no_message_fields(world):
    queued = await world.seed_question(question_id="q-new")

    assert await world.review_store.get_questions(["q-new"]) == [queued]
    doc = stored(world, "q-new")
    assert set(doc) == model_fields()
    assert (doc["message_platform"], doc["message_channel"], doc["message_id"]) == (None, None, None)


async def test_a_write_round_trips_the_moved_question(world):
    asked = await world.seed_question(QuestionStatus.ASKED, question_id="q1")

    assert await world.review_store.record_reply("q1", "still true", AT) is ReplyRecord.RECORDED

    assert await world.review_store.get_questions(["q1"]) == [asked.with_reply("still true", AT)]


async def test_no_stored_document_has_a_field_named_expires_at(world):
    for i, status in enumerate(QuestionStatus):
        await world.seed_question(status, question_id=f"q{i}")
    await world.review_store.close("q0", ClosedBy.REVIEW, None, None, AT)

    assert world.question_docs and all("expires_at" not in doc for doc in world.question_docs.values())


# ------------------------------------------------------------------------------------------------ reads

async def test_get_questions_keeps_the_order_asked_skips_missing_and_repeats(world):
    a = await world.seed_question(question_id="a")
    b = await world.seed_question(question_id="b")

    assert await world.review_store.get_questions(["b", "missing", "a", "b"]) == [b, a]
    assert await world.review_store.get_questions([]) == []


async def test_questions_for_account_returns_only_its_open_questions(world):
    queued = await world.seed_question(question_id="q-queued")
    pending = await world.seed_question(QuestionStatus.PENDING, question_id="q-pending")
    asked = await world.seed_question(QuestionStatus.ASKED, question_id="q-asked")
    unanswered = await world.seed_question(QuestionStatus.UNANSWERED, question_id="q-unanswered")
    await world.seed_question(QuestionStatus.CLOSED, question_id="q-closed")
    await world.seed_question(question_id="q-other", account_id="acc2")

    found = await world.review_store.questions_for_account("acc")

    assert sorted(found, key=lambda q: q.question_id) == sorted([queued, pending, asked, unanswered], key=lambda q: q.question_id)


async def test_all_open_spans_accounts_and_leaves_closed_out(world):
    await world.seed_question(question_id="q1")
    await world.seed_question(QuestionStatus.ASKED, question_id="q2", account_id="acc2")
    await world.seed_question(QuestionStatus.CLOSED, question_id="q3")

    assert sorted(q.question_id for q in await world.review_store.all_open()) == ["q1", "q2"]


async def test_by_message_finds_only_the_same_user_platform_channel_and_id(world):
    ts = REF.message_id
    target = await world.seed_question(QuestionStatus.ASKED, question_id="target")
    await world.seed_question(QuestionStatus.ASKED, question_id="other-channel",
                              message_ref=MessageRef("slack", "D2", ts))
    await world.seed_question(QuestionStatus.ASKED, question_id="other-platform",
                              message_ref=MessageRef("telegram", REF.channel_id, ts))
    other_user = await world.seed_question(QuestionStatus.ASKED, question_id="other-user", user_id="u2",
                                           session_id="u2:D1")
    await world.seed_question(QuestionStatus.ASKED, question_id="other-id",
                              message_ref=MessageRef("slack", REF.channel_id, "1729760000.000999"))

    store = world.review_store
    assert await store.by_message("u1", MessageRef("slack", REF.channel_id, ts)) == target
    assert await store.by_message("u2", MessageRef("slack", REF.channel_id, ts)) == other_user
    assert await store.by_message("u1", MessageRef("slack", "D9", ts)) is None              # another channel's same Slack ts
    assert await store.by_message("u3", MessageRef("slack", REF.channel_id, ts)) is None
    assert await store.by_message("u1", MessageRef("slack", REF.channel_id, "")) is None


async def test_by_message_also_finds_a_closed_question(world):
    closed = await world.seed_question(QuestionStatus.CLOSED, question_id="done")

    assert await world.review_store.by_message("u1", REF) == closed


# ------------------------------------------------------------------------------------------------ indexes

async def test_each_query_is_served_without_a_composite_index(world):
    """The fake refuses a query no declared index serves (`equality_and_in_without_composite` is the parity scenario
    for these shapes); a control query that does need a composite is refused on the same database."""
    await world.seed_question(QuestionStatus.ASKED, question_id="q1")
    store = world.review_store
    col = world.store.db.collection(world.env.fact_review_questions_collection)
    assert world.store.db.declared is not None

    await store.questions_for_account("acc")
    await store.all_open()
    await store.by_message("u1", REF)

    open_values = ["queued", "pending", "asked", "unanswered"]
    assert col.executed == [
        [("where", "account_id", "==", "acc"), ("where", "status", "in", open_values)],
        [("where", "status", "in", open_values)],
        [("where", "user_id", "==", "u1"), ("where", "message_platform", "==", REF.platform),
         ("where", "message_channel", "==", REF.channel_id), ("where", "message_id", "==", REF.message_id)],
    ]
    with pytest.raises(FailedPrecondition):
        await col.where("account_id", "==", "acc").order_by("queue_key").get()


# ------------------------------------------------------------------------------------------------ replies

async def test_a_reply_is_taken_on_an_asked_or_unanswered_question(world):
    await world.seed_question(QuestionStatus.ASKED, question_id="asked")
    await world.seed_question(QuestionStatus.UNANSWERED, question_id="unanswered")

    assert await world.review_store.record_reply("asked", "  yes, still  ", AT) is ReplyRecord.RECORDED
    assert await world.review_store.record_reply("unanswered", "no", AT) is ReplyRecord.RECORDED

    assert stored(world, "asked")["reply_text"] == "yes, still" and stored(world, "asked")["reply_at"] == AT
    assert stored(world, "unanswered")["reply_text"] == "no"


async def test_a_second_reply_is_appended_on_a_new_line(world):
    await world.seed_question(QuestionStatus.ASKED, question_id="q1")
    later = AT + timedelta(minutes=3)

    await world.review_store.record_reply("q1", "first", AT)
    assert await world.review_store.record_reply("q1", "second", later) is ReplyRecord.RECORDED

    assert stored(world, "q1")["reply_text"] == "first\nsecond" and stored(world, "q1")["reply_at"] == later


@pytest.mark.parametrize("status,over", [
    (QuestionStatus.QUEUED, {}),
    (QuestionStatus.PENDING, {"posted": True}),
    (QuestionStatus.CLOSED, {}),
])
async def test_a_reply_is_refused_on_a_question_not_open_and_sent(world, status, over):
    await world.seed_question(status, question_id="q1", **over)
    before = dict(stored(world, "q1"))

    assert await world.review_store.record_reply("q1", "words", AT) is ReplyRecord.REFUSED
    assert stored(world, "q1") == before


async def test_a_reply_is_refused_once_the_review_was_shown_the_reply(world):
    await world.seed_question(QuestionStatus.ASKED, question_id="q1")
    await world.review_store.record_reply("q1", "first", AT)
    assert await world.review_store.mark_offered("q1", AT) is True
    before = dict(stored(world, "q1"))

    assert await world.review_store.record_reply("q1", "more", AT) is ReplyRecord.REFUSED
    assert stored(world, "q1") == before


async def test_a_reply_with_no_words_is_not_recorded(world):
    await world.seed_question(QuestionStatus.ASKED, question_id="q1")

    assert await world.review_store.record_reply("q1", "   ", AT) is ReplyRecord.REFUSED
    assert stored(world, "q1")["reply_text"] is None


async def test_the_reply_is_bounded_and_a_reply_that_no_longer_fits_is_false(world):
    await world.seed_question(QuestionStatus.ASKED, question_id="q1")
    store = world.review_store

    assert await store.record_reply("q1", "a" * (MAX_REPLY_CHARS - 10), AT) is ReplyRecord.RECORDED
    assert await store.record_reply("q1", "b" * 50, AT + timedelta(minutes=1)) is ReplyRecord.RECORDED
    kept = stored(world, "q1")["reply_text"]
    assert len(kept) == MAX_REPLY_CHARS and kept.endswith("\n" + "b" * 9)
    before = dict(stored(world, "q1"))

    assert await store.record_reply("q1", "c" * 20, AT + timedelta(minutes=2)) is ReplyRecord.REFUSED
    assert stored(world, "q1") == before


# ------------------------------------------------------------------------------------------------ ref, offered, close

async def test_the_message_ref_is_set_once_on_an_appended_pending_question(world):
    await world.seed_question(QuestionStatus.PENDING, question_id="q1")
    ref = MessageRef("telegram", "123", "77")

    assert await world.review_store.set_message_ref("q1", ref) is True
    assert await world.review_store.set_message_ref("q1", MessageRef("telegram", "123", "78")) is False

    doc = stored(world, "q1")
    assert (doc["message_platform"], doc["message_channel"], doc["message_id"]) == ("telegram", "123", "77")
    [back] = await world.review_store.get_questions(["q1"])
    assert back.message_ref == ref and back.status is QuestionStatus.PENDING


async def test_the_message_ref_is_refused_before_the_frame_was_appended(world):
    await world.seed_question(QuestionStatus.PENDING, question_id="q1", appended_at=None)

    assert await world.review_store.set_message_ref("q1", REF) is False
    assert stored(world, "q1")["message_id"] is None


async def test_mark_offered_needs_a_reply_not_yet_shown(world):
    await world.seed_question(QuestionStatus.ASKED, question_id="q1")
    assert await world.review_store.mark_offered("q1", AT) is False

    await world.review_store.record_reply("q1", "yes", AT)
    assert await world.review_store.mark_offered("q1", AT) is True
    assert await world.review_store.mark_offered("q1", AT) is False
    assert stored(world, "q1")["offered_at"] == AT


async def test_purge_after_is_set_only_by_the_close(world):
    await world.seed_question(QuestionStatus.PENDING, question_id="posted")
    await world.seed_question(QuestionStatus.ASKED, question_id="q1")
    store = world.review_store
    assert await store.set_message_ref("posted", REF) is True
    assert await store.record_reply("q1", "it ended", NOW) is ReplyRecord.RECORDED
    assert await store.mark_offered("q1", NOW) is True
    assert "purge_after" not in stored(world, "posted") and "purge_after" not in stored(world, "q1")

    assert await store.close("q1", ClosedBy.REVIEW, None, "review", AT) is True

    doc = stored(world, "q1")
    assert doc["status"] == "closed" and doc["closed_at"] == AT and doc["purge_after"] == AT + QUESTION_RETENTION


async def test_an_owner_close_keeps_the_answer_and_a_second_close_is_refused(world):
    await world.seed_question(QuestionStatus.ASKED, question_id="q1")
    store = world.review_store

    assert await store.close("q1", ClosedBy.OWNER, AnswerKind.STILL_TRUE, "button", AT) is True
    assert await store.close("q1", ClosedBy.TIMEOUT, None, None, AT + timedelta(days=1)) is False

    doc = stored(world, "q1")
    assert (doc["closed_by"], doc["answer"], doc["answer_source"], doc["closed_at"]) == ("owner", "still_true", "button", AT)


async def test_a_close_the_model_refuses_is_false_and_changes_nothing(world):
    await world.seed_question(QuestionStatus.ASKED, question_id="q1")
    before = dict(stored(world, "q1"))

    assert await world.review_store.close("q1", ClosedBy.OWNER, None, "digit", AT) is False     # an owner close needs an answer
    assert stored(world, "q1") == before


async def test_every_write_on_a_missing_question_is_false(world):
    store = world.review_store

    assert await store.set_message_ref("nope", REF) is False
    assert await store.record_reply("nope", "x", AT) is ReplyRecord.REFUSED
    assert await store.mark_offered("nope", AT) is False
    assert await store.close("nope", ClosedBy.REVIEW, None, None, AT) is False
    assert world.question_docs == {}


# ------------------------------------------------------------------------------------------------ unreadable documents

async def test_an_invalid_stored_document_is_skipped_and_logged_by_every_list_read(world, caplog):
    caplog.set_level(logging.ERROR)
    good = await world.seed_question(question_id="good")
    col = world.store.db.collection(world.env.fact_review_questions_collection)
    await col.document("broken").set({"question_id": "broken", "account_id": "acc", "status": "queued"})

    assert await world.review_store.questions_for_account("acc") == [good]
    assert await world.review_store.all_open() == [good]
    assert await world.review_store.get_questions(["broken", "good"]) == [good]

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 3 and all("broken" in r.getMessage() for r in errors)


async def test_a_write_on_an_invalid_stored_document_is_logged_and_raised(world, caplog):
    caplog.set_level(logging.ERROR)
    col = world.store.db.collection(world.env.fact_review_questions_collection)
    await col.document("broken").set({"question_id": "broken", "account_id": "acc", "status": "asked"})

    with pytest.raises(ValidationError):
        await world.review_store.record_reply("broken", "x", AT)

    assert any("broken" in r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)
    assert world.question_docs["broken"] == {"question_id": "broken", "account_id": "acc", "status": "asked"}

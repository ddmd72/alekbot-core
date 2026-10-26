"""Facts S4 · T4a2: the review store's queue and current question (plan §2.2, §3.7, §3.13), the real adapter on the
review world's evaluating Firestore fake. Claims and releases are transactions over the state document and the
questions; the decisions are the domain's (`current_question`, `may_send`, `next_in_queue`, `sent_today`).

Reviewer edit (S4 T8e re-review): `close_and_release` returns `QuestionClose` (T8e review F1), so each close asserts
the exact member: RELEASED where the closed question was the current one, CLOSED where it was not, REFUSED for False."""
import asyncio
import logging
from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from src.adapters.firestore_fact_review_store import question_document
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_question import ClosedBy, QuestionStatus
from src.domain.fact_review_question_close import QuestionClose
from src.domain.fact_review_rules import MAX_QUESTIONS_PER_DAY
from src.domain.fact_review_state import FactReviewState
from src.domain.messaging import MessageRef
from tests.support.fact_review_world import ReviewWorld
from tests.support.review_rule_builders import NOW, REF
from tests.support.review_rule_builders import question as build_question

TODAY = date(2026, 10, 24)
TOMORROW = TODAY + timedelta(days=1)
SESSION = "u1:D1"
AT = NOW + timedelta(minutes=1)


@pytest.fixture
def world():
    return ReviewWorld()


def doc(world, question_id):
    return world.question_docs[question_id]


async def queued(world, question_id, days_stale=30, **over):
    """A queued question whose fact has been stale `days_stale` days (more days = earlier in the queue)."""
    return await world.seed_question(question_id=question_id, queue_key=NOW - timedelta(days=days_stale), **over)


async def deliver(world, question_id):
    """The send of a claimed question up to the post: the frame appended, the ref recorded."""
    assert await world.review_store.mark_appended(question_id, AT) is True
    assert await world.review_store.set_message_ref(question_id, REF) is True


# ------------------------------------------------------------------------------------------------ create and state

async def test_create_queued_creates_once(world):
    question = build_question(question_id="q1")

    assert await world.review_store.create_queued(question) is True
    assert await world.review_store.create_queued(question.model_copy(update={"quoted_text": "other"})) is False

    assert await world.review_store.get_questions(["q1"]) == [question]


async def test_create_queued_refuses_a_question_that_is_not_queued(world):
    with pytest.raises(ValueError):
        await world.review_store.create_queued(build_question(QuestionStatus.ASKED, question_id="q1"))
    assert world.question_docs == {}


async def test_get_state_round_trips_every_field_and_is_none_when_absent(world):
    assert await world.review_store.get_state("acc") is None
    state = await world.seed_state(
        owner_user_id="u1", enqueued_at=NOW, started_at=NOW, last_review_at=NOW, last_failed_at=NOW,
        unsuccessful_since=NOW, offers={"f1": 2}, last_digest_at=NOW, undigested_since=NOW, oldest_alert_at=NOW,
        queue_alert_at=NOW, current_question_id="q1", sent_on=TODAY, sent=3, delivery_failed_on=TODAY,
        message_language="uk", labels={"uk": {"yes": "так"}},
    )
    assert all(getattr(state, name) is not None for name in FactReviewState.model_fields)

    assert await world.review_store.get_state("acc") == state
    assert world.state_doc()["sent_on"] == "2026-10-24" and world.state_doc()["labels"] == {"uk": {"yes": "так"}}
    assert "expires_at" not in world.state_doc()


# ------------------------------------------------------------------------------------------------ claim

async def test_a_claim_takes_the_front_of_the_queue_and_points_the_state_at_it(world):
    await queued(world, "younger", days_stale=5)
    front = await queued(world, "front", days_stale=40)

    claimed = await world.review_store.claim_next("acc", TODAY, SESSION)

    assert claimed == front.claimed(SESSION)
    assert doc(world, "front")["status"] == "pending" and doc(world, "front")["session_id"] == SESSION
    assert doc(world, "younger")["status"] == "queued"
    state = await world.review_store.get_state("acc")
    assert (state.current_question_id, state.sent, state.sent_on) == ("front", 1, TODAY)


async def test_a_claim_with_nothing_queued_writes_nothing(world):
    await world.seed_question(QuestionStatus.CLOSED, question_id="done")

    assert await world.review_store.claim_next("acc", TODAY, SESSION) is None
    assert world.state_doc() is None


async def test_a_claim_keeps_the_other_writers_fields_of_the_state(world):
    await world.seed_state(owner_user_id="u1", offers={"f9": 1}, last_review_at=NOW, message_language="en")
    await queued(world, "q1")

    await world.review_store.claim_next("acc", TODAY, SESSION)

    state = await world.review_store.get_state("acc")
    assert (state.owner_user_id, dict(state.offers), state.last_review_at, state.message_language) == ("u1", {"f9": 1}, NOW, "en")


async def test_no_claim_while_a_question_is_out(world):
    await queued(world, "q1")
    await queued(world, "q2", days_stale=10)
    assert (await world.review_store.claim_next("acc", TODAY, SESSION)).question_id == "q1"

    assert await world.review_store.claim_next("acc", TODAY, SESSION) is None
    await deliver(world, "q1")
    await world.review_store.mark_asked("q1", AT)
    assert await world.review_store.claim_next("acc", TODAY, SESSION) is None
    assert doc(world, "q2")["status"] == "queued"


async def test_two_concurrent_claims_give_one_question(world):
    await queued(world, "q1")
    await queued(world, "q2", days_stale=10)
    world.store.db.collection(world.env.fact_review_state_collection).yield_on_get = True   # both read before either commits

    results = await asyncio.gather(*(world.review_store.claim_next("acc", TODAY, f"u1:D{i}") for i in range(2)))

    assert sorted(r is None for r in results) == [False, True]
    assert [q for q, d in world.question_docs.items() if d["status"] == "pending"] == ["q1"]
    state = await world.review_store.get_state("acc")
    assert (state.current_question_id, state.sent) == ("q1", 1)


async def test_the_sixth_claim_of_a_day_gives_none(world):
    for i in range(MAX_QUESTIONS_PER_DAY + 1):
        await queued(world, f"q{i}", days_stale=50 - i)
    store = world.review_store

    for i in range(MAX_QUESTIONS_PER_DAY):
        claimed = await store.claim_next("acc", TODAY, SESSION)
        assert claimed is not None and claimed.question_id == f"q{i}"
        assert await store.close_and_release(claimed.question_id, ClosedBy.REVIEW, None, None, AT) is QuestionClose.RELEASED

    assert await store.claim_next("acc", TODAY, SESSION) is None
    assert doc(world, f"q{MAX_QUESTIONS_PER_DAY}")["status"] == "queued"
    assert (await store.get_state("acc")).sent == MAX_QUESTIONS_PER_DAY

    nxt = await store.claim_next("acc", TOMORROW, SESSION)       # a new day counts from 1
    assert nxt is not None and nxt.question_id == f"q{MAX_QUESTIONS_PER_DAY}"
    state = await store.get_state("acc")
    assert (state.sent, state.sent_on) == (1, TOMORROW)


async def test_after_a_failed_delivery_no_claim_today_and_the_same_question_tomorrow(world):
    await queued(world, "q1")
    await queued(world, "q2", days_stale=10)
    store = world.review_store
    claimed = await store.claim_next("acc", TODAY, SESSION)
    await store.mark_appended(claimed.question_id, AT)               # the frame went in; the post then failed

    assert await store.requeue_front("q1", failed_today=True) is True

    state = await store.get_state("acc")
    assert (state.current_question_id, state.delivery_failed_on) == (None, TODAY)
    assert doc(world, "q1")["status"] == "queued"
    assert await store.claim_next("acc", TODAY, SESSION) is None
    again = await store.claim_next("acc", TOMORROW, SESSION)
    assert again is not None and again.question_id == "q1"


async def test_a_queued_question_closed_while_the_claim_reads_the_queue_is_not_sent(world):
    first = await queued(world, "q1", days_stale=40)
    await queued(world, "q2", days_stale=10)
    closed = first.closed(ClosedBy.RESOLVED_ELSEWHERE, AT)      # any close of a queued question: it writes no state
    world.store.db.interfere_on_txn_query(world.env.fact_review_questions_collection, "q1", question_document(closed))

    claimed = await world.review_store.claim_next("acc", TODAY, SESSION)

    assert claimed is not None and claimed.question_id == "q2"       # the retry saw q1 closed and took the next one
    assert doc(world, "q1")["status"] == "closed" and doc(world, "q1")["closed_by"] == "resolved_elsewhere"
    state = await world.review_store.get_state("acc")
    assert (state.current_question_id, state.sent) == ("q2", 1)


# ------------------------------------------------------------------------------------------------ the send

async def test_the_send_moves_pending_to_asked_and_keeps_the_pointer(world):
    await queued(world, "q1")
    await world.review_store.claim_next("acc", TODAY, SESSION)
    await deliver(world, "q1")

    assert await world.review_store.mark_asked("q1", AT) is True

    assert doc(world, "q1")["status"] == "asked" and doc(world, "q1")["asked_at"] == AT
    assert (await world.review_store.get_state("acc")).current_question_id == "q1"


async def test_mark_appended_is_once(world):
    await world.seed_question(QuestionStatus.PENDING, question_id="q1", appended_at=None)

    assert await world.review_store.mark_appended("q1", AT) is True
    assert await world.review_store.mark_appended("q1", AT + timedelta(minutes=1)) is False
    assert doc(world, "q1")["appended_at"] == AT


async def test_mark_asked_is_refused_unless_pending_and_posted(world):
    await world.seed_question(QuestionStatus.PENDING, question_id="unposted")
    await world.seed_question(QuestionStatus.QUEUED, question_id="queued")

    assert await world.review_store.mark_asked("unposted", AT) is False
    assert await world.review_store.mark_asked("queued", AT) is False
    assert doc(world, "unposted")["status"] == "pending" and doc(world, "queued")["status"] == "queued"


async def test_a_question_the_owner_closed_while_pending_stays_closed_after_mark_asked(world):
    await queued(world, "q1")
    store = world.review_store
    await store.claim_next("acc", TODAY, SESSION)
    await deliver(world, "q1")
    assert await store.close_and_release("q1", ClosedBy.OWNER, AnswerKind.ENDED, "button", AT) is QuestionClose.RELEASED

    assert await store.mark_asked("q1", AT + timedelta(seconds=1)) is False

    stored = doc(world, "q1")
    assert (stored["status"], stored["closed_by"], stored["answer"], stored["asked_at"]) == ("closed", "owner", "ended", None)
    assert (await store.get_state("acc")).current_question_id is None


async def test_an_owner_close_racing_mark_asked_stays_closed(world):
    await queued(world, "q1")
    store = world.review_store
    await store.claim_next("acc", TODAY, SESSION)
    await deliver(world, "q1")
    world.store.db.collection(world.env.fact_review_questions_collection).yield_on_get = True   # both read q1 first

    closed, asked = await asyncio.gather(
        store.close_and_release("q1", ClosedBy.OWNER, AnswerKind.ENDED, "button", AT),
        store.mark_asked("q1", AT + timedelta(seconds=1)),
    )

    assert closed is QuestionClose.RELEASED and asked is False
    stored = doc(world, "q1")
    assert (stored["status"], stored["closed_by"], stored["answer"], stored["asked_at"]) == ("closed", "owner", "ended", None)
    assert (await store.get_state("acc")).current_question_id is None


async def test_a_pending_question_with_a_ref_goes_through_complete_delivered(world):
    await queued(world, "q1")
    store = world.review_store
    await store.claim_next("acc", TODAY, SESSION)
    await deliver(world, "q1")                                       # posted; the move to asked was lost

    assert await store.complete_delivered("q1") is True

    stored = doc(world, "q1")
    assert stored["status"] == "asked" and stored["asked_at"] == AT == stored["appended_at"]
    assert await store.complete_delivered("q1") is False


async def test_complete_delivered_refuses_a_pending_question_never_posted(world):
    await world.seed_question(QuestionStatus.PENDING, question_id="q1")

    assert await world.review_store.complete_delivered("q1") is False
    assert doc(world, "q1")["status"] == "pending"


# ------------------------------------------------------------------------------------------------ releasing the slot

async def test_requeue_front_keeps_the_queue_key_and_clears_the_pointer(world):
    original = await queued(world, "q1", days_stale=33)
    store = world.review_store
    await store.claim_next("acc", TODAY, SESSION)
    await store.mark_appended("q1", AT)

    assert await store.requeue_front("q1", failed_today=False) is True

    [back] = await store.get_questions(["q1"])
    assert back == original                                          # place kept, the attempt forgotten
    state = await store.get_state("acc")
    assert (state.current_question_id, state.delivery_failed_on, state.sent) == (None, None, 1)


async def test_requeue_front_refuses_a_posted_question_and_leaves_the_state(world):
    await queued(world, "q1")
    store = world.review_store
    await store.claim_next("acc", TODAY, SESSION)
    await deliver(world, "q1")
    before = dict(world.state_doc())

    assert await store.requeue_front("q1", failed_today=True) is False

    assert world.state_doc() == before and doc(world, "q1")["status"] == "pending"


async def test_requeue_front_leaves_a_pointer_that_names_another_question(world):
    await world.seed_state(current_question_id="other", sent=1, sent_on=TODAY)
    await world.seed_question(QuestionStatus.PENDING, question_id="q1")

    assert await world.review_store.requeue_front("q1", failed_today=False) is True

    assert (await world.review_store.get_state("acc")).current_question_id == "other"


async def test_close_and_release_frees_the_slot_only_for_the_current_question(world):
    await queued(world, "current", days_stale=40)
    await queued(world, "other", days_stale=10)
    store = world.review_store
    await store.claim_next("acc", TODAY, SESSION)

    assert await store.close_and_release("other", ClosedBy.RESOLVED_ELSEWHERE, None, None, AT) is QuestionClose.CLOSED
    assert (await store.get_state("acc")).current_question_id == "current"

    assert await store.close_and_release("current", ClosedBy.REVIEW, None, None, AT) is QuestionClose.RELEASED
    assert (await store.get_state("acc")).current_question_id is None
    assert doc(world, "current")["status"] == doc(world, "other")["status"] == "closed"


async def test_a_refused_close_and_release_leaves_the_pointer(world):
    await queued(world, "q1")
    store = world.review_store
    await store.claim_next("acc", TODAY, SESSION)

    assert await store.close_and_release("q1", ClosedBy.TIMEOUT, None, None, AT) is QuestionClose.REFUSED   # never asked: no timeout

    assert (await store.get_state("acc")).current_question_id == "q1" and doc(world, "q1")["status"] == "pending"


async def test_close_alone_does_not_touch_the_pointer(world):
    await queued(world, "q1")
    store = world.review_store
    await store.claim_next("acc", TODAY, SESSION)

    assert await store.close("q1", ClosedBy.REVIEW, None, None, AT) is True

    assert (await store.get_state("acc")).current_question_id == "q1"


async def test_mark_unanswered_clears_the_pointer_of_the_current_question_only(world):
    await world.seed_state(current_question_id="q1", sent=1, sent_on=TODAY)
    await world.seed_question(QuestionStatus.ASKED, question_id="q1")
    await world.seed_question(QuestionStatus.ASKED, question_id="q2", message_ref=MessageRef("slack", "D1", "2"))
    store = world.review_store

    assert await store.mark_unanswered("q2") is True
    assert (await store.get_state("acc")).current_question_id == "q1"
    assert await store.mark_unanswered("q1") is True
    assert (await store.get_state("acc")).current_question_id is None
    assert await store.mark_unanswered("q1") is False
    assert doc(world, "q1")["status"] == doc(world, "q2")["status"] == "unanswered"


async def test_a_release_without_a_state_document_creates_none(world):
    await world.seed_question(QuestionStatus.ASKED, question_id="q1")

    assert await world.review_store.mark_unanswered("q1") is True
    assert world.state_doc() is None


async def test_every_queue_write_on_a_missing_question_is_false(world):
    store = world.review_store

    assert await store.mark_appended("nope", AT) is False
    assert await store.mark_asked("nope", AT) is False
    assert await store.complete_delivered("nope") is False
    assert await store.requeue_front("nope", failed_today=True) is False
    assert await store.mark_unanswered("nope") is False
    assert await store.close_and_release("nope", ClosedBy.REVIEW, None, None, AT) is QuestionClose.REFUSED
    assert world.question_docs == {} and world.state_doc() is None


async def test_a_close_racing_a_claim_does_not_clear_the_new_pointer(world):
    await world.seed_question(QuestionStatus.UNANSWERED, question_id="q1")
    await queued(world, "q2", days_stale=10)
    await world.seed_state(current_question_id="q1", sent=1, sent_on=TODAY)   # a pointer to an unanswered question holds nothing
    world.store.db.collection(world.env.fact_review_state_collection).yield_on_get = True   # both read the state first
    store = world.review_store

    claimed, closed = await asyncio.gather(
        store.claim_next("acc", TODAY, SESSION),
        store.close_and_release("q1", ClosedBy.TIMEOUT, None, None, AT),
    )

    assert claimed is not None and claimed.question_id == "q2"
    assert closed is QuestionClose.CLOSED                      # the retry saw the pointer on q2: nothing to release
    assert (await store.get_state("acc")).current_question_id == "q2"
    assert doc(world, "q1")["status"] == "closed" and doc(world, "q2")["status"] == "pending"


# ------------------------------------------------------------------------------------------------ unreadable documents

async def test_a_claim_skips_an_unreadable_question_and_claims_the_next(world, caplog):
    caplog.set_level(logging.ERROR)
    col = world.store.db.collection(world.env.fact_review_questions_collection)
    await col.document("broken").set({"question_id": "broken", "account_id": "acc", "status": "queued"})
    await queued(world, "q1")

    claimed = await world.review_store.claim_next("acc", TODAY, SESSION)

    assert claimed is not None and claimed.question_id == "q1"
    assert any("broken" in r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)


async def test_an_unreadable_state_is_raised_not_overwritten(world, caplog):
    caplog.set_level(logging.ERROR)
    raw = {"account_id": "acc", "sent": -1}
    await world.store.db.collection(world.env.fact_review_state_collection).document("acc").set(raw)
    await queued(world, "q1")

    with pytest.raises(ValidationError):
        await world.review_store.claim_next("acc", TODAY, SESSION)
    with pytest.raises(ValidationError):
        await world.review_store.get_state("acc")

    assert world.state_doc() == raw and doc(world, "q1")["status"] == "queued"
    assert any(r.levelno == logging.ERROR for r in caplog.records)


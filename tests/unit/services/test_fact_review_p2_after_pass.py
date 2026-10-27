"""Facts P2 · card B: what lands after the pass besides the decisions (P2-2): the folds of a queued ask, the Cabinet
questions of routine invoices, and the housekeeping of those questions. The plain service on the review world; the
coordinator answers a fixed pass result (the scope's own rules are card C's)."""
import logging
from datetime import timedelta
from typing import Any, Callable, Optional, Sequence

from src.domain.agent import AgentResponse
from src.domain.entities import ExpiryKind, FactState
from src.domain.fact_review_ask import AskRequest
from src.domain.fact_review_invoice import InvoiceRequest
from src.domain.fact_review_question import ClosedBy, QuestionStatus
from src.domain.fact_review_result import FactReviewResult, encode_review_result
from src.domain.fact_review_task import decode_review_task
from src.domain.pass_actor import PassActor
from tests.support.fact_kit import ACC
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import ReviewWorld
from tests.unit.services.test_fact_review_selection import USER, Rig, day_row, open_questions, stale, stored_question

TALK = "Alex gives a talk at the Hamburg conference"
TRAIN = "Alex books a train to Hamburg"
HOTEL = "Alex books a hotel in Hamburg"
PHONE_BILL = "The phone bill of October arrived, payment not confirmed"
TEXTS = {"happened": "Gave the talk at the Hamburg conference", "not_happened": "Did not give the talk",
         "unknown": "Planned the talk at the Hamburg conference; unknown whether it took place"}


class FixedPass:
    """The coordinator: answers `result` (built from the batch it is given), after an optional `before` hook."""

    def __init__(self, world: ReviewWorld, build: Callable[[Sequence[str]], FactReviewResult],
                 before: Optional[Callable[[], None]] = None) -> None:
        self.world, self.build, self.before = world, build, before
        self.routed = []

    async def route_message(self, message: Any) -> AgentResponse:
        self.routed.append(message)
        if self.before is not None:
            self.before()
        batch = [item.fact.id for item in decode_review_task(message.payload).items]
        return AgentResponse.success(message.task_id, message.recipient, encode_review_result(self.build(batch)))


def ask_of(fact_id: str, *folds: str) -> AskRequest:
    return AskRequest(fact_id=fact_id, fallback="history", question="happened", texts=dict(TEXTS), folds=tuple(folds))


async def plan_facts(world: ReviewWorld):
    """Three stale facts of one plan: the talk (asked), a train and a hotel (parts of it)."""
    talk = await stale(world, TALK, NOW - timedelta(days=9))
    train = await stale(world, TRAIN, NOW - timedelta(days=8))
    hotel = await stale(world, HOTEL, NOW - timedelta(days=7))
    await world.seed_state(account_id=ACC, offers={talk: 1, train: 1, hotel: 1})
    return talk, train, hotel


def rig_with(world: ReviewWorld, build, before=None) -> Rig:
    r = Rig(world)
    r.service._coordinator = FixedPass(world, build, before)
    return r


class TestFolds:
    async def test_a_queued_ask_supersedes_its_folds_counts_them_and_clears_their_offers(self):
        world = ReviewWorld()
        talk, train, hotel = await plan_facts(world)
        r = rig_with(world, lambda batch: FactReviewResult(asks=(ask_of(talk, train, hotel),), kept=()))

        assert await r.run() == {"status": "completed"}

        assert world.docs[train]["state"] == "superseded" and world.docs[train]["superseded_by"] == talk
        assert world.docs[hotel]["state"] == "superseded" and world.docs[hotel]["superseded_by"] == talk
        assert world.docs[train]["lifecycle_events"][-1]["reason"] == f"superseded by {talk}"
        assert world.docs[train]["lifecycle_events"][-1]["actor"] == PassActor.REVIEW.value
        assert world.docs[talk]["state"] == "current"                              # the asked fact is current while it waits
        [q] = [q for q in await open_questions(world) if q.fact_id == talk]
        assert q.question == "happened" and q.fallback == "history" and q.texts == TEXTS
        row = await day_row(world)
        assert (row.queued, row.folded) == (1, 2)
        assert dict((await world.review_store.get_state(ACC)).offers) == {}

    async def test_a_failed_queue_folds_nothing_and_keeps_every_offer(self):
        world = ReviewWorld()
        talk, train, hotel = await plan_facts(world)

        def talk_settled_elsewhere() -> None:
            world.docs[talk]["state"] = "archived"

        r = rig_with(world, lambda batch: FactReviewResult(asks=(ask_of(talk, train),)), talk_settled_elsewhere)

        await r.run()

        assert world.docs[train]["state"] == "stale"
        assert not any(q.fact_id == talk for q in await open_questions(world))
        assert r.service._fact_updates.calls == []
        assert (await day_row(world)) is None or (await day_row(world)).folded == 0
        assert dict((await world.review_store.get_state(ACC)).offers)[train] == 2     # offered by the pass, not cleared

    async def test_a_refused_fold_stays_stale_with_a_warning_and_the_others_still_fold(self, caplog):
        world = ReviewWorld()
        talk, train, hotel = await plan_facts(world)

        def train_closed_meanwhile() -> None:
            world.docs[train]["state"] = "archived"

        r = rig_with(world, lambda batch: FactReviewResult(asks=(ask_of(talk, train, hotel),)), train_closed_meanwhile)

        with caplog.at_level(logging.WARNING):
            await r.run()

        assert world.docs[train]["state"] == "archived"                            # untouched by the fold
        assert world.docs[hotel]["state"] == "superseded"
        assert (await day_row(world)).folded == 1
        ours = [rec.getMessage() for rec in caplog.records if rec.getMessage().startswith("[FactReview]")]
        assert any(train[:8] in line and "fold" in line for line in ours)
        assert not any(TRAIN in line for line in ours)                              # a fact's text is never logged

    async def test_an_exception_in_a_fold_is_a_warning_and_the_run_goes_on(self, caplog):
        world = ReviewWorld()
        talk, train, hotel = await plan_facts(world)
        r = rig_with(world, lambda batch: FactReviewResult(asks=(ask_of(talk, train, hotel),)))
        r.service._fact_updates.script(RuntimeError("store down"))

        with caplog.at_level(logging.WARNING):
            assert await r.run() == {"status": "completed"}

        assert world.docs[train]["state"] == "stale" and world.docs[hotel]["state"] == "superseded"
        assert (await day_row(world)).folded == 1


async def written_invoice(world: ReviewWorld, state: FactState = FactState.CURRENT) -> str:
    return await world.seed(PHONE_BILL, state=state, expiry_kind=ExpiryKind.NONE, reported_date=NOW - timedelta(days=1))


class TestInvoices:
    async def test_a_routine_invoice_gets_one_cabinet_question_on_the_written_version_and_nothing_is_sent(self):
        world = ReviewWorld()
        await world.notification_state.save_primary(USER, "slack", "D1")
        bill = await stale(world, "The phone bill of October arrived", NOW - timedelta(days=9))
        written = await written_invoice(world)
        await world.seed_state(account_id=ACC, offers={bill: 1})
        r = rig_with(world, lambda batch: FactReviewResult(
            invoices=(InvoiceRequest(bill, written, "The October phone bill is paid", "The October phone bill is not paid"),)))

        assert await r.run() == {"status": "completed"}

        [q] = await open_questions(world)
        assert (q.fact_id, q.question, q.delivery, q.origin, q.fallback) == (written, "paid", "digest", "review", "history")
        assert q.texts == {"paid": "The October phone bill is paid", "not_paid": "The October phone bill is not paid"}
        assert q.status is QuestionStatus.QUEUED and not q.in_chat
        state = await world.review_store.get_state(ACC)
        assert state.current_question_id is None and state.sent == 0              # no question went out
        assert [text for _, _, text in world.channels.sent] == [                   # only the digest, which lists it
            "Invoices with no payment confirmation:\n• «The phone bill of October arrived, payment not confirmed»\n"
            "Mark the paid ones in the Cabinet:\nhttps://cabinet.example/cabinet?tab=memory"]
        assert state.last_digest_at == NOW

    async def test_creating_an_invoice_question_marks_the_account_undigested(self):
        world = ReviewWorld()
        bill = await stale(world, "The phone bill of October arrived", NOW - timedelta(days=9))
        written = await written_invoice(world)
        await world.seed_state(account_id=ACC, last_digest_at=NOW - timedelta(days=2))    # the digest is not due yet
        r = rig_with(world, lambda batch: FactReviewResult(
            invoices=(InvoiceRequest(bill, written, "paid", "not paid"),)))

        await r.run()

        assert (await world.review_store.get_state(ACC)).undigested_since == NOW
        assert world.channels.sent == []

    async def test_a_request_for_a_fact_outside_the_batch_is_ignored(self):
        world = ReviewWorld()
        await stale(world, "The phone bill of October arrived", NOW - timedelta(days=9))
        written = await written_invoice(world)
        await world.seed_state(account_id=ACC)
        r = rig_with(world, lambda batch: FactReviewResult(
            invoices=(InvoiceRequest("not-in-the-batch", written, "paid", "not paid"),)))

        await r.run()

        assert await open_questions(world) == []
        assert (await world.review_store.get_state(ACC)).undigested_since is None

    async def test_a_written_version_that_is_gone_or_not_current_is_skipped_with_a_warning(self, caplog):
        world = ReviewWorld()
        bill = await stale(world, "The phone bill of October arrived", NOW - timedelta(days=9))
        archived = await written_invoice(world, FactState.ARCHIVED)
        await world.seed_state(account_id=ACC)
        r = rig_with(world, lambda batch: FactReviewResult(invoices=(
            InvoiceRequest(bill, archived, "paid", "not paid"), InvoiceRequest(bill, "no-such-fact", "paid", "not paid"))))

        with caplog.at_level(logging.WARNING):
            await r.run()

        assert await open_questions(world) == []
        assert sum("invoice" in rec.getMessage() for rec in caplog.records if rec.levelno == logging.WARNING) == 2


async def digest_question(world: ReviewWorld, fact_id: str, queued_days_ago: float, question_id: str = "qd"):
    return await world.seed_question(
        QuestionStatus.QUEUED, question_id=question_id, account_id=ACC, user_id=USER, fact_id=fact_id,
        quoted_text=world.docs[fact_id]["text"], question="paid", delivery="digest", fallback="history",
        texts={"paid": "paid", "not_paid": "not paid"}, queue_key=NOW - timedelta(days=queued_days_ago),
        queued_at=NOW - timedelta(days=queued_days_ago))


class TestDigestQuestionHousekeeping:
    async def test_a_digest_question_closes_resolved_elsewhere_after_ninety_days(self):
        world = ReviewWorld()
        written = await written_invoice(world)
        await digest_question(world, written, 90)
        await world.seed_state(account_id=ACC)

        await Rig(world).run()

        question = await stored_question(world, "qd")
        assert question.status is QuestionStatus.CLOSED and question.closed_by is ClosedBy.RESOLVED_ELSEWHERE

    async def test_a_young_digest_question_with_its_fact_in_place_stays_open(self):
        world = ReviewWorld()
        written = await written_invoice(world)
        await digest_question(world, written, 89)
        await world.seed_state(account_id=ACC)

        await Rig(world).run()

        assert (await stored_question(world, "qd")).status is QuestionStatus.QUEUED

    async def test_a_digest_question_whose_fact_moved_on_closes_resolved_elsewhere(self):
        world = ReviewWorld()
        written = await written_invoice(world)
        await digest_question(world, written, 3)
        world.docs[written]["state"] = "archived"
        await world.seed_state(account_id=ACC)

        await Rig(world).run()

        assert (await stored_question(world, "qd")).closed_by is ClosedBy.RESOLVED_ELSEWHERE

    async def test_a_chat_question_is_not_touched_by_that_housekeeping(self):
        world = ReviewWorld()
        fact = await world.seed(TALK, expiry_kind=ExpiryKind.DATE, expires_at=NOW + timedelta(days=3))
        await world.seed_question(QuestionStatus.QUEUED, question_id="qc", account_id=ACC, user_id=USER, fact_id=fact,
                                  quoted_text=TALK, queued_at=NOW - timedelta(days=200))
        await world.seed_state(account_id=ACC)

        await Rig(world).run()

        assert (await stored_question(world, "qc")).status is QuestionStatus.QUEUED

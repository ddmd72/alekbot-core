"""`FactReviewService.run`: timeouts, facts kept twice and the after-pass fallback (Facts S4 · T8c, plan §3.2 steps 6, 7
and 11's fallback; §3.8; S4-D13, S4-D19, S4-D20).

The plain service on the review world, with the scripted consolidation agent of the T8b tests: the real review store,
lease, counters, repository, `FactAnswerService`, texts and owner zone over one evaluating Firestore fake. Assertions are on
the stored facts, questions, state, counters and tasks. NOW is 2026-10-23 07:30 UTC, 08:30 in the owner's zone."""
from datetime import timedelta
from typing import Any, List

import pytest

from src.domain.entities import ExpiryKind, FactState
from src.domain.fact_answer_result import AnswerResult
from src.domain.fact_lifecycle import REVIEW_ACCEPTS
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_question import QUESTION_TIMEOUT, ClosedBy, QuestionStatus
from src.domain.fact_review_reasons import kept_twice_reason
from src.domain.fact_review_rules import revives_in_window
from src.domain.fact_review_task import FactReviewTask
from src.infrastructure.fact_review_scope import FactReviewScope
from tests.support.fact_kit import ACC
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import ReviewWorld
from tests.support.fact_store import embed
from tests.unit.services.test_fact_review_selection import (
    USER,
    Rig,
    batch_ids,
    current,
    day_row,
    on,
    open_questions,
    stale,
    stored_question,
)

SUBJECT = "Alex reads at the Example Library"


async def timing_out(world: ReviewWorld, question_id: str, fact_id: str, asked_days_ago: float = 15,
                     status: QuestionStatus = QuestionStatus.UNANSWERED, **over: Any):
    """A sent question about `fact_id`, asked `asked_days_ago` days before the world's now."""
    return await world.seed_question(status, question_id=question_id, account_id=ACC, user_id=USER, fact_id=fact_id,
                                     quoted_text=world.docs[fact_id]["text"],
                                     asked_at=world.now - timedelta(days=asked_days_ago), **over)


def last_event(world: ReviewWorld, fact_id: str) -> dict:
    return world.docs[fact_id]["lifecycle_events"][-1]


def fallback_events(world: ReviewWorld, fact_id: str) -> List[dict]:
    return [e for e in world.docs[fact_id]["lifecycle_events"] if e.get("actor") == "timeout"]


# ------------------------------------------------------------------------------------------------ which questions time out

class TestWhichQuestionsTimeOut:
    @pytest.mark.parametrize("status", [QuestionStatus.ASKED, QuestionStatus.UNANSWERED])
    @pytest.mark.parametrize("replied", [False, True], ids=["no_reply", "reply_shown"])
    async def test_a_sent_question_times_out_fourteen_days_after_it_was_asked_reply_or_not(self, status, replied):
        world = ReviewWorld()
        fact = await current(world, SUBJECT)
        asked_at = NOW - QUESTION_TIMEOUT
        reply = dict(reply_text="I think so", reply_at=asked_at + timedelta(days=1),
                     offered_at=asked_at + timedelta(days=2)) if replied else {}
        await world.seed_question(status, question_id="qt", account_id=ACC, user_id=USER, fact_id=fact,
                                  quoted_text=SUBJECT, asked_at=asked_at, **reply)
        await world.seed_state(account_id=ACC)
        r = Rig(world)

        assert await r.run() == {"status": "completed"}

        question = await stored_question(world, "qt")
        assert question.status is QuestionStatus.CLOSED and question.closed_by is ClosedBy.TIMEOUT
        assert last_event(world, fact)["reason"] == "no answer to q=qt: revive"
        assert world.docs[fact]["expires_at"] == NOW + timedelta(days=120)       # twice the 60-day window, from now
        assert (await day_row(world)).timed_out == 1
        assert r.agent.routed == []                                               # no newer neighbour: no model call

    @pytest.mark.parametrize("status", [QuestionStatus.ASKED, QuestionStatus.UNANSWERED])
    @pytest.mark.parametrize("replied", [False, True], ids=["no_reply", "reply_shown"])
    async def test_a_minute_before_the_fourteen_days_nothing_times_out(self, status, replied):
        world = ReviewWorld()
        fact = await current(world, SUBJECT)
        asked_at = NOW - QUESTION_TIMEOUT + timedelta(minutes=1)
        reply = dict(reply_text="I think so", reply_at=asked_at + timedelta(days=1),
                     offered_at=asked_at + timedelta(days=2)) if replied else {}
        await world.seed_question(status, question_id="qt", account_id=ACC, user_id=USER, fact_id=fact,
                                  quoted_text=SUBJECT, asked_at=asked_at, **reply)
        await world.seed_state(account_id=ACC)
        events_before = list(world.docs[fact]["lifecycle_events"])
        r = Rig(world)

        assert await r.run() == {"status": "completed"}

        question = await stored_question(world, "qt")
        assert question.status is QuestionStatus.UNANSWERED                       # step 5: an earlier day's; still open
        assert world.docs[fact]["lifecycle_events"] == events_before
        row = await day_row(world)
        assert row is None or row.timed_out == 0

    async def test_a_queued_question_never_times_out(self):
        world = ReviewWorld()
        fact = await current(world, SUBJECT)
        await world.seed_question(QuestionStatus.QUEUED, question_id="qq", account_id=ACC, user_id=USER, fact_id=fact,
                                  quoted_text=SUBJECT, queued_at=NOW - timedelta(days=60))
        await world.seed_state(account_id=ACC)
        events_before = list(world.docs[fact]["lifecycle_events"])

        assert await Rig(world).run() == {"status": "completed"}

        assert (await stored_question(world, "qq")).status is QuestionStatus.QUEUED
        assert world.docs[fact]["lifecycle_events"] == events_before and world.docs[fact]["state"] == "current"

    async def test_an_unrelated_reply_does_not_shorten_the_timeout(self):
        world = ReviewWorld()
        fact = await current(world, SUBJECT)
        await timing_out(world, "qt", fact, asked_days_ago=10, reply_text="the tram was late today",
                         reply_at=NOW - timedelta(days=1))
        await world.seed_state(account_id=ACC)
        r = Rig(world)

        await r.run()                                                             # the reply is shown; the pass decides nothing

        assert batch_ids(r.agent.tasks[0]) == [fact]
        question = await stored_question(world, "qt")
        assert question.status is QuestionStatus.UNANSWERED and question.offered_at == NOW
        assert fallback_events(world, fact) == []

        world.advance(days=4)                                                     # fourteen days from asked_at
        await r.run()

        assert len(r.agent.routed) == 1                                           # shown once; no newer neighbour
        question = await stored_question(world, "qt")
        assert question.status is QuestionStatus.CLOSED and question.closed_by is ClosedBy.TIMEOUT
        assert last_event(world, fact)["reason"] == "no answer to q=qt: revive"


# ------------------------------------------------------------------------------------------------ S4-D19

class TestTheNeighbourCheck:
    async def test_a_neighbour_created_after_the_question_sends_the_fact_to_the_model_and_undecided_it_falls_back(self):
        world = ReviewWorld()
        fact = await current(world, SUBJECT)
        await timing_out(world, "qt", fact)
        await world.seed("Alex renewed the Example Library card", created_at=NOW - timedelta(days=3))
        await world.seed_state(account_id=ACC)
        r = Rig(world)

        assert await r.run() == {"status": "completed"}

        (task,) = r.agent.tasks
        assert batch_ids(task) == [fact]
        (shown,) = task.questions
        assert shown.question_id == "qt" and shown.is_timed_out(NOW)               # the pass sees it as timed out
        question = await stored_question(world, "qt")
        assert question.status is QuestionStatus.CLOSED and question.closed_by is ClosedBy.TIMEOUT
        assert len(fallback_events(world, fact)) == 1                             # once, after the pass
        assert (await day_row(world)).timed_out == 1
        assert dict((await world.review_store.get_state(ACC)).offers) == {}       # a current candidate is no offer

    async def test_a_candidate_the_pass_decides_gets_no_fallback(self):
        world = ReviewWorld()
        fact = await current(world, SUBJECT)
        await timing_out(world, "qt", fact)
        ev = await world.seed("Alex returned the last library book and closed the card",   # said since the question
                              reported_date=NOW - timedelta(days=1), created_at=NOW - timedelta(days=1))
        await world.seed_state(account_id=ACC)
        r = Rig(world)

        # P2 revised (P2-2): a standing fact a neighbour shows ended is superseded by it, never archived on evidence
        async def play(task: FactReviewTask, scope: FactReviewScope) -> None:
            result = await scope.update_fact({"fact_id": fact, "updates": {"state": "superseded", "superseded_by": ev}})
            assert result["status"] == "updated", result

        r.agent.play = play
        await r.run()

        question = await stored_question(world, "qt")
        assert question.closed_by is ClosedBy.REVIEW
        assert world.docs[fact]["state"] == "superseded" and fallback_events(world, fact) == []
        row = await day_row(world)
        assert (row.timed_out, row.superseded) == (0, 1)

    async def test_neighbours_older_than_the_question_mean_the_fallback_now_with_no_model_call(self):
        world = ReviewWorld()
        fact = await current(world, SUBJECT)
        await timing_out(world, "qt", fact)
        await world.seed("Alex walks to Main Square")                             # created long before the question
        await world.seed_state(account_id=ACC)
        r = Rig(world)

        await r.run()

        assert r.agent.routed == []
        assert (await stored_question(world, "qt")).closed_by is ClosedBy.TIMEOUT
        assert last_event(world, fact)["reason"] == "no answer to q=qt: revive"

    async def test_a_newer_fact_outside_the_five_nearest_does_not_send_the_fact_to_the_model(self):
        world = ReviewWorld()
        fact = await current(world, SUBJECT)
        await timing_out(world, "qt", fact)
        for n in range(5):                                                        # nearer to it than anything else
            await world.seed(f"Alex's library note {n}", vector=embed(SUBJECT))
        await world.seed("Alex trains at the Iron Temple gym", created_at=NOW - timedelta(days=1))
        await world.seed_state(account_id=ACC)
        r = Rig(world)

        await r.run()

        assert r.agent.routed == []
        assert (await stored_question(world, "qt")).closed_by is ClosedBy.TIMEOUT

    async def test_a_reply_not_yet_shown_when_the_question_times_out_is_shown_once_then_the_fallback(self):
        world = ReviewWorld()
        fact = await current(world, SUBJECT)
        await timing_out(world, "qt", fact, reply_text="hm", reply_at=NOW - timedelta(days=1))   # a reply not yet shown
        other = await stale(world, "Alex keeps a weight log", NOW - timedelta(days=3))
        await world.seed_state(account_id=ACC)
        r = Rig(world)

        assert await r.run() == {"status": "completed"}                           # the pass decides nothing

        (task,) = r.agent.tasks
        assert set(batch_ids(task)) == {fact, other}
        (shown,) = task.questions
        assert shown.question_id == "qt" and shown.is_timed_out(NOW) and shown.reply_text == "hm"
        question = await stored_question(world, "qt")
        assert question.closed_by is ClosedBy.TIMEOUT and question.offered_at == NOW
        assert len(fallback_events(world, fact)) == 1                             # once, after the pass
        assert (await day_row(world)).timed_out == 1

    async def test_a_reply_not_yet_shown_at_the_timeout_that_the_pass_decides_on_gets_no_fallback(self):
        world = ReviewWorld()
        fact = await current(world, SUBJECT)
        await timing_out(world, "qt", fact, reply_text="no, I stopped going there in the summer",
                         reply_at=NOW - timedelta(days=1))
        await world.seed_state(account_id=ACC)
        r = Rig(world)

        async def play(task: FactReviewTask, scope: FactReviewScope) -> None:
            basis = {"kind": "reply", "question_id": "qt", "quote": "I stopped going there"}
            result = await scope.update_fact({"fact_id": fact, "updates": {"state": "archived"}, "basis": basis})
            assert result["status"] == "updated", result

        r.agent.play = play
        assert await r.run() == {"status": "completed"}

        question = await stored_question(world, "qt")
        assert question.closed_by is ClosedBy.REVIEW and question.answer_source == "review"
        assert question.offered_at == NOW
        assert world.docs[fact]["state"] == "archived" and fallback_events(world, fact) == []
        row = await day_row(world)
        assert (row.timed_out, row.archived_by_review, row.closed_by_reply) == (0, 1, 1)


# ------------------------------------------------------------------------------------------------ resolved elsewhere

class TestResolvedElsewhere:
    async def test_a_fact_rewritten_meanwhile_closes_its_question_resolved_elsewhere(self):
        world = ReviewWorld()
        new = await world.seed("Alex works at Acme", lineage_id="lin-job", version=2)
        old = await world.seed("Alex is a data engineer", lineage_id="lin-job", version=1, state=FactState.SUPERSEDED,
                               is_current=False, superseded_by=new)
        await timing_out(world, "qt", old)
        await world.seed_state(account_id=ACC)
        new_before = dict(world.docs[new])
        r = Rig(world)

        assert await r.run() == {"status": "completed"}

        question = await stored_question(world, "qt")
        assert question.status is QuestionStatus.CLOSED and question.closed_by is ClosedBy.RESOLVED_ELSEWHERE
        assert world.docs[new] == new_before and world.docs[old]["state"] == "superseded"
        assert r.agent.routed == []
        row = await day_row(world)
        assert row is None or row.timed_out == 0

    async def test_a_fact_closed_meanwhile_closes_its_question_resolved_elsewhere(self):
        world = ReviewWorld()
        fact = await current(world, SUBJECT)
        await timing_out(world, "qt", fact)
        world.docs[fact]["state"] = "archived"
        await world.seed_state(account_id=ACC)

        await Rig(world).run()

        assert (await stored_question(world, "qt")).closed_by is ClosedBy.RESOLVED_ELSEWHERE
        assert world.docs[fact]["state"] == "archived" and fallback_events(world, fact) == []


# ------------------------------------------------------------------------------------------------ the fallback

class TestTheFallback:
    async def test_every_silence_doubles_the_window_up_to_a_year(self):
        world = ReviewWorld()
        fact = await world.seed(SUBJECT, expiry_kind=ExpiryKind.HORIZON, ttl_days=14,
                                reported_date=NOW - timedelta(days=14), expires_at=NOW)
        await world.seed_state(account_id=ACC)
        r = Rig(world)
        windows = []

        for n in range(6):
            await timing_out(world, f"q{n}", fact, asked_days_ago=14)
            assert await r.run() == {"status": "completed"}
            assert (await stored_question(world, f"q{n}")).closed_by is ClosedBy.TIMEOUT
            expires = world.docs[fact]["expires_at"]
            windows.append((expires - world.now).days)
            world.now = expires                                                   # the next silence, when it falls due

        assert windows == [28, 56, 112, 224, 365, 365]
        assert world.docs[fact]["reported_date"] == NOW - timedelta(days=14) and world.docs[fact]["state"] == "current"

    async def test_a_fallback_write_that_loses_its_precondition_leaves_the_fact_for_the_next_run(self):
        world = ReviewWorld()
        fact = await current(world, SUBJECT)
        await timing_out(world, "qt", fact)
        await world.seed_state(account_id=ACC)
        r = Rig(world)
        real = world.repo.update_fact_fields
        raced: List[str] = []

        async def racing(stored: Any, fields: Any) -> None:
            if stored.fact.id == fact and not raced:        # another writer lands between the read and the write
                raced.append(fact)
                await real(await world.repo.resolve_current(fact, ACC, REVIEW_ACCEPTS), {"last_updated": world.now})
            await real(stored, fields)

        world.repo.update_fact_fields = racing  # type: ignore[method-assign]

        assert await r.run() == {"status": "completed"}

        assert raced == [fact]
        assert (await stored_question(world, "qt")).status is QuestionStatus.UNANSWERED
        assert fallback_events(world, fact) == [] and world.docs[fact]["expires_at"] == NOW + timedelta(days=20)
        row = await day_row(world)
        assert row is None or row.timed_out == 0

        world.advance(hours=1)
        assert await r.run() == {"status": "completed"}

        assert (await stored_question(world, "qt")).closed_by is ClosedBy.TIMEOUT
        assert len(fallback_events(world, fact)) == 1


# ------------------------------------------------------------------------------------------------ kept twice (S4-D20)

class TestKeptTwice:
    async def test_a_fact_kept_twice_gets_a_code_question_and_is_queued_never_revived(self):
        world = ReviewWorld()
        since = NOW - timedelta(days=20)
        fact = await stale(world, "Alex orders a cookbook", since)
        expires_before = world.docs[fact]["expires_at"]
        await world.seed_state(account_id=ACC, offers={fact: 2})
        r = Rig(world)

        assert await r.run() == {"status": "completed"}

        (question,) = await open_questions(world)
        assert question.status is QuestionStatus.QUEUED and question.fact_id == fact
        assert question.fallback == "revive" and question.quoted_text == "Alex orders a cookbook"
        assert question.queue_key == since and question.queued_at == NOW
        doc = world.docs[fact]
        assert doc["state"] == "current" and doc["stale_since"] is None and doc["expires_at"] == expires_before
        event = last_event(world, fact)
        assert (event["from"], event["to"], event["actor"]) == ("stale", "current", "review_fallback")
        assert event["reason"] == kept_twice_reason(question.question_id)
        assert revives_in_window(doc["lifecycle_events"], NOW) == 0
        assert dict((await world.review_store.get_state(ACC)).offers) == {}
        assert r.agent.routed == []                                               # queued by code, not offered again
        row = await day_row(world)
        assert (row.queued, row.revived) == (1, 0)
        assert world.sink.texts == []

    async def test_a_dated_fact_kept_twice_falls_back_to_history(self):
        world = ReviewWorld()
        fact = await world.seed_stale("Alex plans a weekend in Coimbra")         # a date decision that has passed
        assert world.docs[fact]["expiry_kind"] == ExpiryKind.DATE.value
        await world.seed_state(account_id=ACC, offers={fact: 2})

        await Rig(world).run()

        # P2 revised (C6): a date fact kept twice gets a code-built happened question that falls back to history
        (question,) = await open_questions(world)
        assert question.fallback == "history" and world.docs[fact]["state"] == "current"
        assert (question.question, question.origin, question.texts) == ("happened", "kept_twice", {})

    async def test_a_fact_offered_once_is_offered_again_not_queued(self):
        world = ReviewWorld()
        fact = await stale(world, "Alex orders a cookbook", NOW - timedelta(days=20))
        await world.seed_state(account_id=ACC, offers={fact: 1})
        r = Rig(world)

        await r.run()

        assert await open_questions(world) == []
        assert batch_ids(r.agent.tasks[0]) == [fact] and world.docs[fact]["state"] == "stale"
        assert dict((await world.review_store.get_state(ACC)).offers) == {fact: 2}

    async def test_a_kept_twice_write_that_conflicts_leaves_the_fact_stale_and_its_offers(self):
        world = ReviewWorld()
        fact = await stale(world, "Alex orders a cookbook", NOW - timedelta(days=20))
        await world.seed_state(account_id=ACC, offers={fact: 2})
        r = Rig(world)
        real = world.repo.update_fact_fields
        raced: List[str] = []

        async def racing(stored: Any, fields: Any) -> None:
            if stored.fact.id == fact and not raced:
                raced.append(fact)
                await real(await world.repo.resolve_current(fact, ACC, REVIEW_ACCEPTS), {"last_updated": world.now})
            await real(stored, fields)

        world.repo.update_fact_fields = racing  # type: ignore[method-assign]

        assert await r.run() == {"status": "completed"}

        assert raced == [fact]
        assert await open_questions(world) == [] and world.docs[fact]["state"] == "stale"
        assert dict((await world.review_store.get_state(ACC)).offers) == {fact: 2}
        assert r.agent.routed == []                                               # kept twice: never offered a third time


# ------------------------------------------------------------------------------------------------ an unanswered question

class TestAnUnansweredQuestion:
    async def test_one_answered_later_in_the_cabinet_closes_and_releases_nothing(self):
        world = ReviewWorld()
        fact = await current(world, SUBJECT)
        await timing_out(world, "q1", fact, asked_days_ago=1, status=QuestionStatus.ASKED)
        waiting = await current(world, "Alex walks to Main Square")
        await world.seed_question(QuestionStatus.QUEUED, question_id="q2", account_id=ACC, user_id=USER,
                                  fact_id=waiting, quoted_text="Alex walks to Main Square")
        await world.seed_state(account_id=ACC, current_question_id="q1", sent_on=(NOW - timedelta(days=1)).date(),
                               sent=1)
        r = Rig(world)

        await r.run()                                                             # step 5: yesterday's question

        assert (await stored_question(world, "q1")).status is QuestionStatus.UNANSWERED
        assert (await world.review_store.get_state(ACC)).current_question_id is None
        enqueued_before = list(world.enqueued)

        world.advance(hours=3)
        answers = r.service._answer_service
        result = await answers.apply(ACC, USER, "q1", AnswerKind.STILL_TRUE, "cabinet", world.now)

        assert result is AnswerResult.APPLIED
        closed = await stored_question(world, "q1")
        assert closed.closed_by is ClosedBy.OWNER and closed.answer_source == "cabinet"
        assert world.enqueued == enqueued_before                                  # no next question released
        assert (await stored_question(world, "q2")).status is QuestionStatus.QUEUED
        assert (await world.review_store.get_state(ACC)).current_question_id is None
        assert world.docs[fact]["state"] == "current"

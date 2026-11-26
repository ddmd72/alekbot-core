"""The review model's answer to a profile question's own point (fact profile RFC §4.7), through the whole run: the pass
holds it, code records it as the owner's own answer and closes the question on that record (source `review`), and the
profile step never opens that point again. On the review world with the real review store and a recording answers
store. Invented cast."""
from datetime import timedelta
from typing import Any

from src.domain.fact_profile import ProfileReviewItem
from src.domain.fact_review_question import ClosedBy, QuestionStatus
from src.domain.profile_open_points import PointFact, open_points
from src.services.profile_question_answers import ProfileQuestionAnswers
from tests.support.fact_kit import ACC
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import ReviewWorld
from tests.support.profile_question_builders import ACME_FACT, LINEAGE, profile_question
from tests.unit.services.test_fact_review_profile_housekeeping import CORRECTED, MEETING
from tests.unit.services.test_fact_review_selection import USER, Rig, day_row, stored_question
from tests.unit.services.test_profile_question_answers import Answers

ALEX_WORDS = "It is me, Alex"
WHEN = "It was the first of October, 2026"


async def replied(world: ReviewWorld, kind: str, fact_id: str, words: str, text: str, **over: Any):
    draft = profile_question(kind, QuestionStatus.UNANSWERED, account_id=ACC, user_id=USER, fact_id=fact_id,
                             quoted_text=text, **over)
    await world.put_question(draft)
    await world.review_store.record_reply(draft.question_id, words, NOW - timedelta(hours=1))


def rig_of(world: ReviewWorld, store: Answers) -> Rig:
    return Rig(world, profile_answers=ProfileQuestionAnswers(store))


async def test_an_accepted_answer_is_recorded_as_the_owners_and_closes_the_question_by_review():
    world, store = ReviewWorld(), Answers()
    fact = await world.seed(MEETING)
    await replied(world, "person", fact, ALEX_WORDS, MEETING)
    await world.seed_state(account_id=ACC)
    rig = rig_of(world, store)
    before = dict(world.docs[fact])

    async def play(task, scope):
        res = await scope.answer_profile_question({"question_id": "pq-person", "answer": "pick1", "quote": "me, Alex"})
        assert res["status"] == "answer_held", res
    rig.agent.play = play

    assert await rig.run() == {"status": "completed"}

    [recorded] = store.recorded
    assert (recorded.resolution, recorded.name, recorded.question_id) == ("entry", "Alex Doe", "pq-person")
    closed = await stored_question(world, "pq-person")
    assert (closed.status, closed.closed_by, closed.answer, closed.answer_source) == (
        QuestionStatus.CLOSED, ClosedBy.REVIEW, None, "review")
    assert world.docs[fact] == before                                 # the point is answered; the fact is untouched
    assert (await day_row(world)).closed_by_reply == 1


async def test_words_that_correct_the_fact_and_settle_the_point_do_both_and_close_once():
    world, store = ReviewWorld(), Answers()
    fact = await world.seed(MEETING)
    await replied(world, "person", fact, ALEX_WORDS, MEETING)
    await world.seed_state(account_id=ACC)
    rig = rig_of(world, store)

    async def play(task, scope):
        wrote = await scope.update_fact({
            "fact_id": fact, "updates": {"state": "current", "content": CORRECTED, "expiry": {"kind": "none"}},
            "basis": {"kind": "reply", "question_id": "pq-person", "quote": "me, Alex"}})
        assert wrote["status"] == "updated", wrote
        answer = await scope.answer_profile_question({"question_id": "pq-person", "answer": "pick1", "quote": "Alex"})
        assert answer["status"] == "answer_held", answer
    rig.agent.play = play

    assert await rig.run() == {"status": "completed"}

    assert [a.resolution for a in store.recorded] == ["entry"]
    assert [d["text"] for d in world.docs.values() if d.get("state") == "current"] == [CORRECTED]
    closed = await stored_question(world, "pq-person")
    assert (closed.closed_by, closed.answer_source) == (ClosedBy.REVIEW, "review")
    row = await day_row(world)
    assert (row.closed_by_reply, row.rewritten) == (1, 1)


async def test_a_question_that_closed_during_the_pass_records_nothing():
    world, store = ReviewWorld(), Answers()
    fact = await world.seed(MEETING)
    await replied(world, "person", fact, ALEX_WORDS, MEETING)
    await world.seed_state(account_id=ACC)
    rig = rig_of(world, store)

    async def play(task, scope):
        await scope.answer_profile_question({"question_id": "pq-person", "answer": "pick1", "quote": "me, Alex"})
        await world.review_store.close_and_release("pq-person", ClosedBy.RESOLVED_ELSEWHERE, None, None, NOW)
    rig.agent.play = play

    await rig.run()

    assert store.recorded == []
    assert (await stored_question(world, "pq-person")).closed_by is ClosedBy.RESOLVED_ELSEWHERE


async def test_an_answer_the_store_cannot_record_closes_nothing_and_the_question_stays_open():
    world, store = ReviewWorld(), Answers()
    fact = await world.seed(MEETING)
    await replied(world, "person", fact, ALEX_WORDS, MEETING)
    await world.seed_state(account_id=ACC)

    async def broken(account_id, answer):
        raise RuntimeError("the answers store is down")
    store.record = broken
    rig = rig_of(world, store)

    async def play(task, scope):
        await scope.answer_profile_question({"question_id": "pq-person", "answer": "pick1", "quote": "me, Alex"})
    rig.agent.play = play

    assert await rig.run() == {"status": "completed"}

    kept = await stored_question(world, "pq-person")
    assert kept.is_open and kept.reply_text == ALEX_WORDS and store.recorded == []


async def test_a_date_answered_from_the_words_is_never_asked_again_by_the_profile_step():
    world, store = ReviewWorld(), Answers()
    fact = await world.seed(ACME_FACT)
    await replied(world, "date", fact, WHEN, ACME_FACT)
    await world.seed_state(account_id=ACC)
    rig = rig_of(world, store)
    view = [PointFact(fact, LINEAGE, ACME_FACT, NOW, (), (ProfileReviewItem("time_points", "on 2026-10-01"),))]
    assert [p.point.printed for p in open_points(view, store.answers, None, None)] == ["on 2026-10-01"]

    async def play(task, scope):
        res = await scope.answer_profile_question({"question_id": "pq-date", "answer": "2026-10-01", "quote": "first of October"})
        assert res["status"] == "answer_held", res
    rig.agent.play = play

    await rig.run()

    [recorded] = store.recorded
    assert (recorded.resolution, recorded.start, recorded.precision) == ("date", "2026-10-01", "day")
    assert open_points(view, store.answers, None, None) == []
    assert (await stored_question(world, "pq-date")).closed_by is ClosedBy.REVIEW

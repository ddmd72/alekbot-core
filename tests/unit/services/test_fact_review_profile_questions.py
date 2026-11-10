"""Profile questions created by the daily review run (fact profile RFC §4.7, PR 5e): after the profile step, inside the
lease, the open points it found are queued up to three open, a question whose point is gone closes `resolved_elsewhere`,
and a profile step that gave no run or ended with an error creates and closes nothing. On the review world with the real
review store; the profile step is a recorder that returns a run. Invented cast."""
import logging
from typing import Any, List, Optional, Sequence

from src.domain.fact_profile_run import FactProfileRun
from src.domain.fact_review_question import ClosedBy, QuestionStatus
from src.domain.fact_review_rules import PROFILE_OPEN_CAP, profile_question_id
from src.domain.profile_open_point import OpenPointOption, ProfileOpenPoint
from src.domain.profile_open_points import OpenPoint
from src.domain.profile_answers import date_point_key, place_point_key
from src.domain.profile_apply import ProfileApply
from src.domain.profile_question_plan import profile_question as profile_question_of
from src.services.profile_question_answers import ProfileQuestionAnswers
from tests.support.fact_review_world import ReviewWorld
from tests.support.profile_question_builders import (
    ACME_FACT, DOE_FACT, NOW, PORTO_FACT, ROME_FACT, acme_point, doe_point, porto_point, profile_question, rome_point,
)
from tests.support.fact_kit import ACC as PACC
from tests.unit.services.test_fact_profile_answers_step import NAMES, Answers
from tests.unit.services.test_fact_profile_service import ATLAS_DIGEST, World, answer, fid, ok
from tests.unit.services.test_fact_review_service_run import ACC, USER, rig

ROME = OpenPoint(rome_point(), "f-rome", ROME_FACT, 3, NOW)
PORTO = OpenPoint(porto_point(), "f-porto", PORTO_FACT, 2, NOW)
DOE = OpenPoint(doe_point(), "f-doe", DOE_FACT, 1, NOW)
ACME = OpenPoint(acme_point(), "f-acme", ACME_FACT, 1, NOW)


class Step:
    def __init__(self, run: Optional[FactProfileRun]) -> None:
        self.run_value = run
        self.collected: List[bool] = []

    async def run(self, account_id, deadline, collect_points=False):
        self.collected.append(collect_points)
        return self.run_value


def with_points(world: ReviewWorld, points: Sequence[OpenPoint], **run: Any):
    r = rig(world)
    step = Step(FactProfileRun(open_points=tuple(points), **run))
    r.service._profile_step = step
    return r, step


async def profile_questions(world: ReviewWorld, scope: str = "profile"):
    return await world.review_store.questions_for_account(ACC, scope=scope)  # type: ignore[arg-type]


async def stored(world: ReviewWorld, question_id: str):
    return (await world.review_store.get_questions([question_id]))[0]


async def test_the_run_asks_the_step_for_its_points_and_queues_at_most_three_most_facts_first():
    world = ReviewWorld()
    r, step = with_points(world, [ROME, PORTO, DOE, ACME])

    assert await r.run() == {"status": "completed"}

    assert step.collected == [True]
    queued = await profile_questions(world)
    assert PROFILE_OPEN_CAP == 3 and len(queued) == 3
    assert {q.question_id for q in queued} == {
        profile_question_id(ACC, p.point.key) for p in (ROME, PORTO, DOE)}
    rome = await stored(world, profile_question_id(ACC, ROME.point.key))
    assert (rome.status, rome.question, rome.origin, rome.fallback) == (QuestionStatus.QUEUED, "place", "profile", "none")
    assert (rome.fact_id, rome.quoted_text, rome.open_point, rome.user_id, rome.account_id) == (
        "f-rome", ROME_FACT, ROME.point, USER, ACC)
    assert rome.queue_key == rome.queued_at == world.now
    assert rome.texts == {} and rome.reply_text is None
    assert (await stored(world, profile_question_id(ACC, DOE.point.key))).question == "person"


async def test_each_kind_gets_its_question_kind():
    world = ReviewWorld()
    r, _ = with_points(world, [ACME, DOE])

    await r.run()

    kinds = {q.open_point.key: q.question for q in await profile_questions(world) if q.open_point}
    assert kinds == {date_point_key("lin-1"): "date", doe_point().key: "person"}


async def test_a_second_run_creates_nothing_twice():
    world = ReviewWorld()
    r, _ = with_points(world, [ROME, PORTO, DOE, ACME])
    await r.run()
    before = {key: dict(doc) for key, doc in world.question_docs.items()}

    await r.run()

    assert {key: dict(doc) for key, doc in world.question_docs.items()} == before


async def test_open_lifecycle_questions_do_not_count_towards_the_cap():
    world = ReviewWorld()
    await world.seed_question(QuestionStatus.QUEUED, question_id="q-life")
    r, _ = with_points(world, [ROME, PORTO, DOE])

    await r.run()

    assert len(await profile_questions(world)) == 3
    assert (await stored(world, "q-life")).status is QuestionStatus.QUEUED


async def test_a_point_whose_question_is_stored_already_is_skipped_and_the_next_point_is_asked():
    world = ReviewWorld()
    old = profile_question("place", QuestionStatus.ASKED, question_id=profile_question_id(ACC, ROME.point.key),
                           account_id=ACC, user_id=USER)
    await world.put_question(old.closed(ClosedBy.TIMEOUT, NOW))
    r, _ = with_points(world, [ROME, PORTO, DOE, ACME])

    await r.run()

    open_ids = {q.question_id for q in await profile_questions(world)}
    assert open_ids == {profile_question_id(ACC, p.point.key) for p in (PORTO, DOE, ACME)}
    assert (await stored(world, profile_question_id(ACC, ROME.point.key))).closed_by is ClosedBy.TIMEOUT


async def test_a_question_whose_point_is_gone_closes_resolved_elsewhere_and_the_others_stay():
    world = ReviewWorld()
    gone = await world.put_question(profile_question(
        "place", QuestionStatus.ASKED, question_id="pq-gone", account_id=ACC, user_id=USER))
    kept = await world.put_question(profile_question(
        "person", QuestionStatus.ASKED, question_id=profile_question_id(ACC, DOE.point.key), account_id=ACC,
        user_id=USER))
    r, _ = with_points(world, [DOE])

    await r.run()

    closed = await stored(world, gone.question_id)
    assert closed.status is QuestionStatus.CLOSED and closed.closed_by is ClosedBy.RESOLVED_ELSEWHERE
    assert closed.answer is None
    assert (await stored(world, kept.question_id)).is_open


async def test_a_run_that_ended_with_an_error_creates_and_closes_nothing(caplog):
    world = ReviewWorld()
    await world.put_question(profile_question("place", QuestionStatus.ASKED, question_id="pq-gone", account_id=ACC,
                                              user_id=USER))
    r, _ = with_points(world, [ROME], error=True)

    with caplog.at_level(logging.INFO):
        assert await r.run() == {"status": "completed"}

    assert [q.question_id for q in await profile_questions(world)] == ["pq-gone"]
    assert (await stored(world, "pq-gone")).is_open
    assert not [rec for rec in caplog.records if "profile questions account=" in rec.getMessage()]


async def test_without_a_run_or_a_step_nothing_is_created():
    world = ReviewWorld()
    r = rig(world)
    r.service._profile_step = Step(None)

    assert await r.run() == {"status": "completed"}
    assert await profile_questions(world, "all") == []

    plain = ReviewWorld()
    assert await rig(plain).run() == {"status": "completed"}
    assert await profile_questions(plain, "all") == []


async def test_the_creation_is_one_log_line_with_counts_and_a_store_error_leaves_the_result(caplog):
    world = ReviewWorld()
    r, _ = with_points(world, [ROME, PORTO])
    with caplog.at_level(logging.INFO):
        await r.run()
    line = [rec.getMessage() for rec in caplog.records if "[FactReview] profile questions account=" in rec.getMessage()]
    assert len(line) == 1 and "points=2 queued=2 closed=0" in line[0]

    broken = ReviewWorld()
    r2, _ = with_points(broken, [ROME])

    async def boom(question):
        raise RuntimeError("firestore unavailable")

    broken.review_store.create_queued = boom  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR):
        assert await r2.run() == {"status": "completed"}
    assert [rec for rec in caplog.records if "profile questions failed" in rec.getMessage()]


async def test_creation_runs_inside_the_lease():
    world = ReviewWorld()
    r, _ = with_points(world, [ROME])
    seen: List[Any] = []
    original = world.review_store.create_queued

    async def watch(question):
        seen.append(world.store.db.collection(world.env.memory_write_leases_collection).docs.get(ACC))
        return await original(question)

    world.review_store.create_queued = watch  # type: ignore[method-assign]
    await r.run()

    assert len(seen) == 1 and seen[0] is not None


# ------------------------------------------------------------------------------------------------ the profile step's points
# (the profile world of `test_fact_profile_answers_step`, with three facts printing Rome and one printing Porto)

async def _points_world(store: Optional[Answers], **over: Any) -> World:
    world = World(profile_answers=store, **over)

    async def atlas():
        return NAMES, ATLAS_DIGEST

    world.service._atlas = atlas
    rome = ok(answer(places=[{"city": "Rome", "country": "", "source": "Rome"}]))
    world.profiler.replies[ROME_FACT] = rome
    world.profiler.replies[PORTO_FACT] = ok(answer(places=[{"city": "Porto", "country": "Portugal", "source": "Porto"}]))
    for n in (1, 2, 3):
        await world.seed(ROME_FACT, id=f"f-rome-{n}", lineage_id=f"lin-rome-{n}", age_days=n)
    await world.seed(PORTO_FACT)
    return world


async def test_a_run_that_collects_carries_the_points_over_the_profiles_it_just_wrote():
    world = await _points_world(Answers())

    run = await world.service.run(PACC, world.until, collect_points=True)

    assert not run.error and run.written == 4
    assert len(run.open_points) == 1
    point = run.open_points[0]
    assert (point.point.key, point.facts, point.fact_id) == (place_point_key("Rome", ""), 3, "f-rome-3")
    assert [o.value for o in point.point.options] == ["1000", "2000"]


async def test_a_run_that_does_not_collect_carries_none():
    world = await _points_world(Answers())

    assert (await world.service.run(PACC, world.until)).open_points == ()


async def test_without_an_answers_store_the_run_carries_none():
    world = await _points_world(None)

    run = await world.service.run(PACC, world.until, collect_points=True)

    assert not run.error and run.open_points == ()


async def test_a_run_that_ended_with_an_error_carries_none():
    world = await _points_world(Answers(fail=True))

    run = await world.service.run(PACC, world.until, collect_points=True)

    assert run.error and run.open_points == ()


async def test_facts_the_run_did_not_write_are_read_as_stored():
    world = await _points_world(Answers())
    await world.service.run(PACC, world.until)
    world.profiler.calls.clear()

    run = await world.service.run(PACC, world.until, collect_points=True)

    assert run.written == 0 and world.profiler.calls == []
    assert [(p.point.key, p.facts) for p in run.open_points] == [(place_point_key("Rome", ""), 3)]


async def test_the_owner_answers_rome_and_the_next_step_re_derives_the_three_facts_only_without_a_call():
    store = Answers()
    world = await _points_world(store)
    run = await world.service.run(PACC, world.until, collect_points=True)
    point = run.open_points[0]
    porto_before = dict(world.docs[fid(PORTO_FACT)])
    world.profiler.calls.clear()

    question = profile_question_of(point, PACC, USER, world.clock.now)
    assert await ProfileQuestionAnswers(store).apply_option(question, "pick1", world.clock.now) is ProfileApply.APPLIED
    scan = await world.service.scan(PACC)
    after = await world.service.run(PACC, world.until, collect_points=True)

    assert (scan.stale_derivation, scan.fresh, scan.to_call) == (3, 1, 0)
    assert world.profiler.calls == [] and after.rederived == 3 and not after.error
    assert after.open_points == ()
    for n in (1, 2, 3):
        assert world.docs[f"f-rome-{n}"]["place_keys"] == ["pl:1000", "co:italy"]
        assert world.docs[f"f-rome-{n}"]["places_unresolved"] == []
    assert world.docs[fid(PORTO_FACT)] == porto_before


async def test_a_dont_remember_answer_removes_the_point_and_it_is_never_asked_again():
    store = Answers()
    world = await _points_world(store)
    point = (await world.service.run(PACC, world.until, collect_points=True)).open_points[0]
    question = profile_question_of(point, PACC, USER, world.clock.now)

    await ProfileQuestionAnswers(store).apply_option(question, "dont_know", world.clock.now)

    assert (await world.service.run(PACC, world.until, collect_points=True)).open_points == ()

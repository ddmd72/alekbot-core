"""The janitor and the profile questions (fact profile RFC §4.7): a queued profile question makes `review_due` say `queue`,
but it is not a stale fact in the review's hands, so it is not in the pool, does not hold its fact's due expiry, and is not
in the standing or the alerts. On the review world with the real services. Invented cast."""
import logging
from datetime import timedelta

from src.domain.fact_review_question import QuestionStatus
from tests.support.fact_kit import ACC
from tests.support.fact_lifecycle_world import PAST
from tests.support.fact_review_world import ReviewWorld
from tests.support.profile_question_builders import profile_question
from tests.unit.services.test_fact_lifecycle_sweep_review import (
    alerts, ask, daily, due, review_tasks, reviewed_recently, run,
)
from tests.unit.services.test_fact_review_selection import USER


async def queued_profile(world: ReviewWorld, kind: str = "place", fact_id: str = "f-place", **over):
    return await world.put_question(profile_question(
        kind, QuestionStatus.QUEUED, account_id=ACC, user_id=USER, fact_id=fact_id, **over))


async def test_a_queued_profile_question_makes_the_review_due_for_the_queue(caplog):
    world = ReviewWorld()
    await reviewed_recently(world)
    await queued_profile(world)

    with caplog.at_level(logging.INFO):
        result = await run(world)

    assert result["review_enqueued"] == 1 and len(review_tasks(world)) == 1
    assert any("review enqueued for account" in r.getMessage() and "(queue)" in r.getMessage() for r in caplog.records)


async def test_without_a_queued_profile_question_nothing_is_enqueued_for_a_reviewed_account():
    world = ReviewWorld()
    await reviewed_recently(world)

    assert (await run(world))["review_enqueued"] == 0 and review_tasks(world) == []


async def test_a_profile_question_does_not_hold_its_facts_due_expiry():
    world = ReviewWorld()
    await reviewed_recently(world)
    fact = await due(world, "Alex has a trip to Rome")
    await queued_profile(world, fact_id=fact)

    await run(world)

    assert world.docs[fact]["state"] == "stale"
    assert daily(world)["staled"] == 1


async def test_a_lifecycle_question_still_holds_its_fact_so_the_contrast_is_the_scope():
    world = ReviewWorld()
    await reviewed_recently(world)
    fact = await due(world, "Alex has a trip to Rome")
    await ask(world, "q1", fact, QuestionStatus.QUEUED)

    await run(world)

    assert world.docs[fact]["state"] == "current"


async def test_profile_questions_are_not_in_the_pool_the_standing_or_the_queue_alert():
    world = ReviewWorld()
    await reviewed_recently(world)
    for i in range(50):
        await queued_profile(world, question_id=f"pq-{i:02d}", fact_id=f"f-{i:02d}")
    free = await due(world, "Hanna lives in Porto", expires_at=PAST - timedelta(days=100))

    await run(world)

    assert world.docs[free]["state"] == "stale"            # a pool of 50 would have left no room: it counts 0
    assert alerts(world) == []                             # 50 queued is over the alert's 35, but they are not stale facts
    assert not any("queue 50" in text for text in world.sink.texts)

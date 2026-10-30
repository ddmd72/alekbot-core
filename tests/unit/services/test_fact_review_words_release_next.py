"""A reply in words to the question that is out lets the next question go the same day (the review's send path with the
real answer service): the owner answers q1 in words, the released `next` task runs, q2 goes."""
from src.domain.fact_review_question import QuestionStatus
from tests.unit.services.test_fact_review_questions import (
    ACC, Rig, USER, next_tasks, posted_ids, state, stored, world_with,
)


async def test_words_to_the_question_out_send_the_next_one_the_same_day():
    world = await world_with("q1", "q2", "q3")
    r = Rig(world)
    await r.run()
    assert posted_ids(world) == ["q1"]
    ref = (await stored(world, "q1")).message_ref
    assert ref is not None

    text = await r.answers.answer_reply(USER, ACC, ref.platform, ref.channel_id, ref.message_id,
                                        "still there, twice a week", world.now)

    assert text is not None
    assert (await stored(world, "q1")).status is QuestionStatus.ASKED
    assert len(next_tasks(world)) == 1
    await r.run("next")

    assert posted_ids(world) == ["q1", "q2"]
    assert (await state(world)).current_question_id == "q2"
    assert (await stored(world, "q3")).status is QuestionStatus.QUEUED

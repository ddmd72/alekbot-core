"""The owner's words to a question that is not a profile question (fact profile RFC §4.7, PR 5e): code reads nothing,
records nothing and says `NOT_READ`. A profile question always carries an open point of the field of its kind (the model
validates it), and place, person and date are all its kinds, so the closing `return None` of the reader has no caller."""
from src.services.profile_question_answers import ProfileApply, ProfileQuestionAnswers
from tests.support.review_rule_builders import question
from tests.unit.services.test_profile_question_answers import Answers
from tests.support.fact_lifecycle_world import NOW


async def test_words_to_a_lifecycle_question_without_an_open_point_are_not_read_and_nothing_is_recorded():
    store = Answers()

    applied = await ProfileQuestionAnswers(store).apply_words(question(), "Rome, Italy", NOW)

    assert applied is ProfileApply.NOT_READ
    assert store.recorded == []

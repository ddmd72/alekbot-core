"""Facts S4 · T11b: the answers a batch may carry (`deciding_answer`, `DECIDING_ANSWERS`)."""
import pytest

from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_answers import DECIDING_ANSWERS, deciding_answer


@pytest.mark.parametrize("raw, kind", [
    ("still_true", AnswerKind.STILL_TRUE), ("ended", AnswerKind.ENDED), ("never", AnswerKind.NEVER),
])
def test_the_three_deciding_answers_are_read_by_their_names(raw, kind):
    assert deciding_answer(raw) is kind


@pytest.mark.parametrize("raw", ["dont_know", "Never", " never", "", "maybe", None, 3, ["never"], AnswerKind.DONT_KNOW])
def test_anything_else_is_not_an_answer(raw):
    assert deciding_answer(raw) is None


def test_dont_know_is_the_one_answer_a_batch_cannot_carry():
    assert set(DECIDING_ANSWERS) == set(AnswerKind) - {AnswerKind.DONT_KNOW}

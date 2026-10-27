"""The fixed message language a review task carries for the pass's translations."""
import pytest

from src.domain.fact_review_task import FactReviewTask, ReviewItem, decode_review_task, encode_review_task
from tests.support.fact_kit import make_fact


def _task(**over):
    return FactReviewTask(items=(ReviewItem(fact=make_fact("Alex plays tennis on Sundays")),), **over)


def test_the_task_round_trips_its_fixed_language():
    task = _task(mirror=False, language="uk")
    assert decode_review_task(encode_review_task(task)).language == "uk"
    assert _task(mirror=False).language is None


def test_a_mirror_task_refuses_a_fixed_language():
    with pytest.raises(ValueError):
        _task(mirror=True, language="uk")


@pytest.mark.parametrize("bad", ["UK", "ukr", "u", "", "u1"])
def test_a_language_that_is_not_a_lowercase_code_is_refused(bad):
    with pytest.raises(ValueError):
        _task(mirror=False, language=bad)

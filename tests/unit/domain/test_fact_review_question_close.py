"""`QuestionClose` replaced a bool return: a truth test on it raises instead of reading every member as success."""
import json

import pytest

from src.domain.fact_review_question_close import QuestionClose


@pytest.mark.parametrize("member", list(QuestionClose))
def test_a_truth_test_raises(member):
    with pytest.raises(TypeError):
        bool(member)
    with pytest.raises(TypeError):
        if member:
            pass


def test_members_still_compare_look_up_and_serialise():
    assert QuestionClose("released") is QuestionClose.RELEASED
    assert QuestionClose.CLOSED == "closed"
    assert QuestionClose.REFUSED in {QuestionClose.REFUSED}
    assert json.dumps(QuestionClose.RELEASED) == '"released"'

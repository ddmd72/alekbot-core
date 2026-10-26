"""`fact_changed_note` (Facts S4 · T8d2, owner decision O3): which results of applying an answer tell the owner that the
fact changed, for a digit and for a button alike."""
import pytest

from src.domain.fact_answer_result import AnswerResult
from src.domain.fact_review_reply import ReplyPath, acknowledgement, fact_changed_note
from src.domain.ui_messages import UIMessage


@pytest.mark.parametrize("result, note", [
    (AnswerResult.CHANGED, UIMessage.MEMORY_FACT_CHANGED),
    (AnswerResult.CONFLICT, UIMessage.MEMORY_FACT_CHANGED),
    (AnswerResult.APPLIED, None),
    (AnswerResult.NOT_OPEN, None),
    (AnswerResult.NOT_APPLIED, None),
    (AnswerResult.NOT_FOUND, None),
])
def test_only_a_changed_fact_or_a_lost_race_has_the_note(result, note):
    assert fact_changed_note(result) is note


@pytest.mark.parametrize("result", list(AnswerResult))
def test_a_digit_says_the_same_line_a_button_does(result):
    if fact_changed_note(result) is not None:
        assert acknowledgement(ReplyPath.DIGIT, result) is fact_changed_note(result)

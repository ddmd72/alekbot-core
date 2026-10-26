"""What the owner's reply to a memory-review question comes to: the three paths a reply can take, which text answers each,
and when a delivered question must first be completed.

A reply binds to a question only through the question's message ref (the store finds the question by it), so the rules
here start from a question that was found. Reading the reply for a digit is the question message's (`digit_answer`); the
path is decided from the question and that reading."""
from enum import Enum
from typing import Optional

from .fact_answer_result import AnswerResult
from .fact_review_answer_kind import AnswerKind
from .fact_review_question import FactReviewQuestion, QuestionStatus
from .ui_messages import UIMessage


class ReplyPath(str, Enum):
    ALREADY_ANSWERED = "already_answered"  # the question is closed: nothing is applied or recorded
    DIGIT = "digit"                        # an open, delivered question and a lone digit: the answer is applied by code
    WORDS = "words"                        # an open, delivered question and anything else: recorded for the review


def reply_path(question: FactReviewQuestion, digit: Optional[AnswerKind]) -> Optional[ReplyPath]:
    """The path of a reply to `question`, `digit` being what the question's message read in it (None for words).
    None when the reply counts for nothing: the question is open but was never delivered."""
    if not question.is_open:
        return ReplyPath.ALREADY_ANSWERED
    if not question.answerable:
        return None
    return ReplyPath.DIGIT if digit is not None else ReplyPath.WORDS


def needs_completing(question: FactReviewQuestion) -> bool:
    """A delivered question whose move to asked was lost (pending, with its message ref): `completed()` first, so its
    `asked_at` stays on the record before the reply is applied or recorded."""
    return question.status is QuestionStatus.PENDING and question.delivered


def acknowledgement(path: ReplyPath, result: Optional[AnswerResult] = None) -> Optional[UIMessage]:
    """The text that answers a reply on `path`, or None when the reply is not answered here (the message then goes on as
    ordinary conversation). A digit's text depends on what applying it came to (`result`): applied; the question closed
    meanwhile; or the fact changed or another writer won (the question stays open, nothing was written). A refused write
    (`not_applied`) and a question that is not this owner's (`not_found`) are not answered here."""
    if path is ReplyPath.ALREADY_ANSWERED:
        return UIMessage.MEMORY_ALREADY_ANSWERED
    if path is ReplyPath.WORDS:
        return UIMessage.MEMORY_REPLY_NOTED
    if result is AnswerResult.APPLIED:
        return UIMessage.MEMORY_ANSWER_NOTED
    if result is AnswerResult.NOT_OPEN:
        return UIMessage.MEMORY_ALREADY_ANSWERED
    if result in (AnswerResult.CHANGED, AnswerResult.CONFLICT):
        return UIMessage.MEMORY_FACT_CHANGED
    return None

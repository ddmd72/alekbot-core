"""What the owner's reply to a memory-review question comes to: the three paths a reply can take, which text answers each,
and when a delivered question must first be completed.

A reply binds to a question only through the question's message ref (the store finds the question by it), so the rules
here start from a question that was found. Reading the reply for a digit is the question message's (`digit_answer`); the
path is decided from the question and that reading."""
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .fact_answer_result import AnswerResult
from .fact_review_answer_kind import AnswerKind
from .fact_review_question import FactReviewQuestion, QuestionStatus
from .messaging import MessageRef
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


class ClickPath(str, Enum):
    OPEN = "open"        # the question is open and the click is on its message: the answer is applied
    CLOSED = "closed"    # the question is closed: the click shows its existing answer, nothing is written
    IGNORED = "ignored"  # the click is on another message than the question's: nothing happens


def click_path(question: FactReviewQuestion, message_ref: MessageRef) -> ClickPath:
    """The path of a click on the message `message_ref`. An open question takes it only on the message it was posted at; a
    closed one also when it has no stored ref (closed during its own post), which the first click then heals."""
    if question.is_open:
        return ClickPath.OPEN if question.message_ref == message_ref else ClickPath.IGNORED
    return ClickPath.CLOSED if question.message_ref in (None, message_ref) else ClickPath.IGNORED


class ClickShow(str, Enum):
    CHOSEN = "chosen"      # the answer was applied: the message shows the chosen option
    KEPT = "kept"          # the fact changed or another writer won: the options stay, with a note
    CLOSED = "closed"      # the question closed meanwhile: it is read again and shown as closed
    NOTHING = "nothing"    # nothing is shown: the write was refused, or the question is not this owner's


@dataclass(frozen=True)
class ClickOutcome:
    """What a click's apply result comes to on the message: what to show, and the note when it keeps the options."""

    show: ClickShow
    note: Optional[UIMessage] = None


def click_outcome(result: AnswerResult) -> ClickOutcome:
    """What the owner's click shows once `apply` has come to `result`."""
    if result is AnswerResult.APPLIED:
        return ClickOutcome(ClickShow.CHOSEN)
    changed = fact_changed_note(result)
    if changed is not None:
        return ClickOutcome(ClickShow.KEPT, changed)
    if result is AnswerResult.NOT_OPEN:
        return ClickOutcome(ClickShow.CLOSED)
    return ClickOutcome(ClickShow.NOTHING)


def needs_completing(question: FactReviewQuestion) -> bool:
    """A delivered question whose move to asked was lost (pending, with its message ref): `completed()` first, so its
    `asked_at` stays on the record before the reply is applied or recorded."""
    return question.status is QuestionStatus.PENDING and question.delivered


def fact_changed_note(result: AnswerResult) -> Optional[UIMessage]:
    """The text that tells the owner his answer was not applied because the fact changed meanwhile (`changed`) or another
    writer won (`conflict`): in both the question stays open and nothing was written. None for any other result."""
    if result in (AnswerResult.CHANGED, AnswerResult.CONFLICT):
        return UIMessage.MEMORY_FACT_CHANGED
    return None


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
    return None if result is None else fact_changed_note(result)

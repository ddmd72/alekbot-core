"""The answer the review model gives to a profile question's own point (fact profile RFC §4.7): the owner's words, which
code could not read, settle which place, person or date his fact means, and the model says so with the id of one of the
options the question showed (or, for a date question, a date in an ISO form) and the words that settle it.

`ProfileAnswerRequest` is what the pass holds; `profile_answer_refusal` is the one check, run when the model calls the tool
and again when code records the answer against the question as stored then. Pure: it decides and writes nothing."""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .fact_review_question import FactReviewQuestion
from .fact_review_rules import quote_in_reply
from .profile_option_answer import profile_option_answer
from .profile_question_message import FIXED_OPTIONS
from .profile_reply_words import read_date_reply

TOOL_ERROR = "Tool error: answer_profile_question: "


@dataclass(frozen=True)
class ProfileAnswerRequest:
    """`question_id`: the profile question; `answer`: an option id the question showed, or a date for a date question;
    `quote`: the owner's words, copied from his reply, that settle the point."""

    question_id: str
    answer: str
    quote: str


def shown_option_ids(question: FactReviewQuestion) -> tuple[str, ...]:
    """The option ids the owner was shown on a profile question: its candidates, then its kind's fixed answers."""
    point = question.open_point
    candidates = tuple(option.id for option in point.options) if point is not None else ()
    return (*candidates, *FIXED_OPTIONS.get(question.question, ()))


def profile_answer_refusal(
    question: FactReviewQuestion, answer: str, quote: str, now: datetime,
) -> Optional[str]:
    """Why `answer` may not be recorded as the owner's answer to `question`, as the text the model reads, or None when it
    may. Refused: a question that is not an open profile question with words recorded; a quote that is not in those
    words; an answer that is no option the question showed (and, for a date question, no ISO date)."""
    if not question.is_profile:
        return f"{TOOL_ERROR}question_id names a question that is not a profile question"
    if not question.is_open:
        return f"{TOOL_ERROR}question_id names a question that is already closed"
    if not question.reply_text:
        return f"{TOOL_ERROR}question_id names a profile question with no recorded words from the owner"
    if not quote_in_reply(quote, question.reply_text):
        return f"{TOOL_ERROR}quote is not in the owner's words; copy the words that settle the point from his reply"
    if profile_option_answer(question, answer, now) is not None:
        return None
    point = question.open_point
    if question.question == "date" and point is not None and read_date_reply(answer, point, question.question_id, now):
        return None
    accepted = ", ".join(shown_option_ids(question))
    if question.question == "date":
        accepted += (
            "; or a date as YYYY, YYYY-MM or YYYY-MM-DD, or a range of two of one precision joined by a dash "
            "(one line, at most 6 words)"
        )
    return f"{TOOL_ERROR}answer is not one this question accepts: {accepted}"

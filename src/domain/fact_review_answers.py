"""The owner's answers to a memory-review question: which digit means what, and how each answer changes its fact.

Digits only: there is no word parsing in code. An answer in words goes to the review model as text."""
from datetime import datetime
from typing import Any, Dict, Final, Optional

from .entities import ExpiryKind, FactEntity, FactState
from .fact_review_answer_change import AnswerChange
from .fact_review_answer_kind import AnswerKind
from .fact_review_question import AnswerSource, FactReviewQuestion
from .fact_review_reasons import answer_reason
from .fact_review_rules import fallback_fields, fallback_window_days, is_dated_plan
from .interactive_message import InteractiveMessage
from .local_time import local_moment

__all__ = ["AnswerKind", "answer_updates", "digit_answer", "ended_text"]

_CODE_QUOTE: Final[str] = "`"


def digit_answer(message: InteractiveMessage, text: str) -> Optional[AnswerKind]:
    """The answer a reply that is only a digit gives, or None (anything else is words, which are the review's).

    The digit is `message.option_for_reply`'s, which owns the digit-to-option rule; this adds only the punctuation a
    chat client puts around a lone digit: a wrapping pair of backticks and one full stop after it (`3`, `2.`).
    The option is the answer of the same name."""
    reply = text.strip()
    if reply.startswith(_CODE_QUOTE) and reply.endswith(_CODE_QUOTE):
        reply = reply[1:-1]
    if reply.endswith("."):
        reply = reply[:-1]
    option_id = message.option_for_reply(reply)
    return None if option_id is None else AnswerKind(option_id)


def ended_text(content: str, at: datetime, tz: str) -> str:
    """The history fact written when the owner says a standing fact has ended: when he said so (month and year in his
    zone) and what it was. The fact is archived beside it."""
    return f"No longer true (the owner said so in {local_moment(at, tz).strftime('%b %Y')}): {content}"


def answer_updates(
    kind: AnswerKind, fact: FactEntity, question: FactReviewQuestion, now: datetime, tz: str, source: AnswerSource
) -> AnswerChange:
    """What the owner's answer does to the fact he was asked about (`source`: where the answer came from).

    `still_true`: current again for the window a silence would give (`fallback_window_days`), counted from now.
    `ended`: archived; a standing fact also gets its `ended_text` as a history fact, a dated plan (`is_dated_plan`) is
    archived only. `never`: invalidated, nothing new. `dont_know`: the question's fallback now, as the owner's
    (`fallback_fields`). The trail reasons are the domain builders'."""
    if kind is AnswerKind.DONT_KNOW:
        return AnswerChange(fields=fallback_fields(fact, question, now, "owner"))
    updates: Dict[str, Any]
    history: Optional[str] = None
    if kind is AnswerKind.STILL_TRUE:
        updates = {
            "state": FactState.CURRENT.value,
            "expiry": {"kind": ExpiryKind.HORIZON.value, "days": fallback_window_days(fact)},
        }
    elif kind is AnswerKind.ENDED:
        updates = {"state": FactState.ARCHIVED.value}
        history = None if is_dated_plan(fact, question) else ended_text(fact.text, now, tz)
    else:
        updates = {"state": FactState.INVALIDATED.value}
    return AnswerChange(
        updates=updates, reason=answer_reason(kind, question.question_id, source), evidence_at=now, history=history
    )

"""The owner's answers to a memory-review question: which digit means what, and how each answer changes its fact.

Digits only: there is no word parsing in code. An answer in words goes to the review model as text."""
from datetime import datetime
from typing import Any, Dict, Final, Mapping, Optional

from .entities import FactEntity, FactState
from .fact_expiry import EXPIRY_NONE
from .fact_lifecycle import compute_expiry, lifecycle_event, with_event
from .fact_review_answer_change import AnswerChange
from .fact_review_answer_kind import AnswerKind
from .fact_review_question import AnswerSource, FactReviewQuestion
from .fact_review_reasons import answer_reason, history_reason
from .fact_review_rules import confirmed_expiry, fallback_fields, is_dated_plan
from .fact_write_context import FactWriteContext, as_utc, reviews_stored_memory
from .interactive_message import InteractiveMessage
from .local_time import local_moment
from .new_fact_attributes import NewFactAttributes

__all__ = [
    "answer_updates", "digit_answer", "ended_text", "history_fact", "history_refusal", "history_trail",
]

# English whatever the process locale: the text is written into a stored fact.
_MONTHS: Final[tuple[str, ...]] = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def digit_answer(message: InteractiveMessage, text: str) -> Optional[AnswerKind]:
    """The answer a reply that is only a digit gives, or None (anything else is words, which are the review's).

    The digit is `message.option_for_reply`'s, which owns the digit-to-option rule and the punctuation around a lone
    digit; the option's id is the answer of the same name. A message with an option that is no answer is a bug."""
    option_id = message.option_for_reply(text)
    return None if option_id is None else AnswerKind(option_id)


def ended_text(content: str, at: datetime, tz: str) -> str:
    """The history fact written when the owner says a standing fact has ended: when he said so (month and year in his
    zone, month names in English whatever the process locale) and what it was. The fact is archived beside it."""
    said = local_moment(at, tz)
    return f"No longer true (the owner said so in {_MONTHS[said.month - 1]} {said.year}): {content}"


def history_fact(old: FactEntity, text: str, at: datetime, tz: str) -> NewFactAttributes:
    """The attributes of the history fact created beside the archive of `old` (S4-D27; `text` is `ended_text`'s or the
    review's sentence): `text` as content, expiry `none`, state current, `reported_date` `at` (when it was said), and
    `old`'s domain, type, tags, temporal class and context priority. No metadata and no context: those described what
    `old` claimed while it held. Its lineage is new (the builder's); its trail entry is `history_trail`'s.

    Raises ValueError for an empty `text`."""
    if not text.strip():
        raise ValueError("a history fact needs its text")
    reported = as_utc(at)
    return NewFactAttributes(
        content=text,
        tags=list(old.tags),
        type=old.type,
        metadata={},
        domain=old.domain,
        temporal_class=old.temporal_class,
        state=FactState.CURRENT,
        context_priority=old.context_priority,
        context=None,
        reported_date=reported,
        expiry=EXPIRY_NONE,
        expiry_fields=compute_expiry(EXPIRY_NONE, reported, tz),
    )


def history_trail(fact: FactEntity, archived_id: str, at: datetime, actor: str) -> FactEntity:
    """`fact` (the built history fact) with its own trail entry: born current (no `from`) at `at`, under `actor`, with
    `history_reason(archived_id)`."""
    entry = lifecycle_event(at, None, FactState.CURRENT, actor, history_reason(archived_id))
    return fact.model_copy(update={"lifecycle_events": with_event(fact.lifecycle_events, entry)})


def history_refusal(
    history: str, updates: Mapping[str, Any], context: FactWriteContext, evidence_at: Optional[datetime]
) -> Optional[str]:
    """Why an `update_fact` call may not carry `history`, as the text of the failed result, or None when it may: only a
    review or owner write (`reviews_stored_memory`) whose `updates` are exactly `{"state": "archived"}` (any case), with
    a non-empty text and `evidence_at` (the history fact's `reported_date`). Nothing is written when it is refused."""
    if not reviews_stored_memory(context):
        return f"history: only the review or the owner write a history fact, not {context.actor}; nothing was written"
    state = updates.get("state")
    archived = isinstance(state, str) and state.strip().lower() == FactState.ARCHIVED.value
    if set(updates) != {"state"} or not archived:
        return 'history goes only with an update that is exactly {"state": "archived"}; nothing was written'
    if not history.strip():
        return "history: the text of the fact to create beside the archive is empty; nothing was written"
    if evidence_at is None:
        return "history needs evidence_at, the moment it was said, as the new fact's reported_date; nothing was written"
    return None


def answer_updates(
    kind: AnswerKind, fact: FactEntity, question: FactReviewQuestion, now: datetime, tz: str, source: AnswerSource
) -> AnswerChange:
    """What the owner's answer does to the fact he was asked about (`source`: where the answer came from).

    `still_true`: current again, keeping the fact's own decision when it is permanent or a date still ahead
    (`confirmed_expiry`), else for the window a silence would give, counted from now.
    `ended`: archived; a standing fact also gets its `ended_text` as a history fact, a dated plan (`is_dated_plan`) is
    archived only. `never`: invalidated, nothing new. `dont_know`: the question's fallback now, as the owner's
    (`fallback_fields`). The trail reasons are the domain builders'."""
    if kind is AnswerKind.DONT_KNOW:
        return AnswerChange(fields=fallback_fields(fact, question, now, "owner"))
    updates: Dict[str, Any]
    history: Optional[str] = None
    if kind is AnswerKind.STILL_TRUE:
        updates = {"state": FactState.CURRENT.value, "expiry": confirmed_expiry(fact, now, tz)}
    elif kind is AnswerKind.ENDED:
        updates = {"state": FactState.ARCHIVED.value}
        history = None if is_dated_plan(fact, question) else ended_text(fact.text, now, tz)
    else:
        updates = {"state": FactState.INVALIDATED.value}
    return AnswerChange(
        updates=updates, reason=answer_reason(kind, question.question_id, source), evidence_at=now, history=history
    )

"""The owner's answers to a memory-review question: which digit means what, and how each answer changes its fact.

Digits only: there is no word parsing in code. An answer in words goes to the review model as text."""
from datetime import datetime
from typing import Final, Optional

from .entities import ContextPriority, FactEntity, FactState, TemporalClass
from .fact_expiry import EXPIRY_NONE
from .fact_lifecycle import compute_expiry, lifecycle_event, walked_to_unseen_text, with_event
from .fact_review_answer_change import AnswerChange
from .fact_review_answer_kind import AnswerKind
from .fact_review_question import AnswerSource, FactReviewQuestion
from .fact_review_reasons import answer_reason, history_reason
from .fact_review_rules import confirmed_expiry, fallback_fields, fallback_revives, is_dated_plan
from .fact_update import ParsedFactUpdate
from .fact_write_context import FactWriteContext, as_utc, reviews_stored_memory
from .interactive_message import InteractiveMessage
from .local_time import local_moment
from .new_fact_attributes import NewFactAttributes

__all__ = [
    "ARCHIVE_ONLY", "DECIDING_ANSWERS", "MAX_BULK_ANSWERS", "answer_closes_fact", "answer_updates", "deciding_answer", "digit_answer", "ended_text", "history_fact", "history_moved_refusal",
    "history_refusal", "history_trail", "option_answer",
]

# The one update a history fact goes with: the fact archived, nothing else changed.
ARCHIVE_ONLY: Final[ParsedFactUpdate] = ParsedFactUpdate(state=FactState.ARCHIVED)

# The answers that say what is true of the fact; `dont_know` only hands it to the question's fallback.
DECIDING_ANSWERS: Final[tuple[AnswerKind, ...]] = (AnswerKind.STILL_TRUE, AnswerKind.ENDED, AnswerKind.NEVER)
# How many questions one batch of answers may name.
MAX_BULK_ANSWERS: Final[int] = 50

# English whatever the process locale: the text is written into a stored fact.
_MONTHS: Final[tuple[str, ...]] = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def digit_answer(message: InteractiveMessage, text: str) -> Optional[AnswerKind]:
    """The answer a reply that is only a digit gives, or None (anything else is words, which are the review's).

    The digit is `message.option_for_reply`'s, which owns the digit-to-option rule and the punctuation around a lone
    digit; the option's id is the answer of the same name. A message with an option that is no answer is a bug."""
    option_id = message.option_for_reply(text)
    return None if option_id is None else AnswerKind(option_id)


def deciding_answer(raw: object) -> Optional[AnswerKind]:
    """The deciding answer `raw` names (`still_true`, `ended` or `never`, exactly), or None for anything else."""
    if not isinstance(raw, str):
        return None
    try:
        kind = AnswerKind(raw)
    except ValueError:
        return None
    return kind if kind in DECIDING_ANSWERS else None


def option_answer(option_id: str) -> Optional[AnswerKind]:
    """The answer an option of the question's message stands for (the option's id is the answer of the same name), or None
    for an id the message does not offer: a callback from a message of another version, or a damaged one."""
    try:
        return AnswerKind(option_id)
    except ValueError:
        return None


def ended_text(content: str, at: datetime, tz: str) -> str:
    """The history fact written when the owner says a standing fact has ended: when he said so (month and year in his
    zone, month names in English whatever the process locale) and what it was. The fact is archived beside it."""
    said = local_moment(at, tz)
    return f"No longer true (the owner said so in {_MONTHS[said.month - 1]} {said.year}): {content}"


def history_fact(old: FactEntity, text: str, at: datetime, tz: str) -> NewFactAttributes:
    """The attributes of the history fact created beside the archive of `old` (S4-D27; `text` is `ended_text`'s or the
    review's sentence): `text` as content, expiry `none`, state current, `reported_date` `at` (when it was said),
    temporal class stable, context priority archival, and `old`'s domain, type and tags. No metadata and no context:
    those described what `old` claimed while it held. Its lineage is new (the builder's); its trail entry is
    `history_trail`'s.

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
        # S4-D27: a lasting record; archival keeps it out of the baseline cache, and it stays searchable.
        temporal_class=TemporalClass.STABLE,
        state=FactState.CURRENT,
        context_priority=ContextPriority.ARCHIVAL,
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
    history: str, update: ParsedFactUpdate, context: FactWriteContext, evidence_at: Optional[datetime]
) -> Optional[str]:
    """Why a write may not carry `history`, as the reason of the failed result, or None when it may: only a review or
    owner write (`reviews_stored_memory`) whose update is an archive and nothing else (`ARCHIVE_ONLY`), with a non-empty
    text and `evidence_at` (the history fact's `reported_date`). Nothing is written when it is refused."""
    if not reviews_stored_memory(context):
        return f"history: only the review or the owner write a history fact, not {context.actor}; nothing was written"
    if update != ARCHIVE_ONLY:
        return "history goes only with an update that archives the fact and changes nothing else; nothing was written"
    if not history.strip():
        return "history: the text of the fact to create beside the archive is empty; nothing was written"
    if evidence_at is None:
        return "history needs evidence_at, the moment it was said, as the new fact's reported_date; nothing was written"
    return None


def history_moved_refusal(shown_text: str, stored: FactEntity, requested_id: str) -> Optional[str]:
    """Why a history archive of `requested_id` may not land on `stored` (the document its id was followed to), or None:
    the fact was rewritten since the owner was asked (`walked_to_unseen_text`; `shown_text` is the text of
    `requested_id`), so the history would describe a text the archived version does not carry."""
    if walked_to_unseen_text(shown_text, stored, requested_id):
        return (
            f"history: fact {requested_id} was rewritten since the owner was asked (now {stored.id}); "
            "nothing was written"
        )
    return None


def answer_closes_fact(kind: AnswerKind, question: FactReviewQuestion) -> bool:
    """Whether the owner's answer `kind` sets the question's fact aside (archived or invalidated): `ended` and `never`
    do; `still_true` does not; `dont_know` does when the question's fallback is to archive. What the digest tells him."""
    if kind is AnswerKind.DONT_KNOW:
        return not fallback_revives(question)
    return kind is not AnswerKind.STILL_TRUE


def answer_updates(
    kind: AnswerKind, fact: FactEntity, question: FactReviewQuestion, now: datetime, tz: str, source: AnswerSource
) -> AnswerChange:
    """What the owner's answer does to the fact he was asked about (`source`: where the answer came from).

    The update is typed (`ParsedFactUpdate`), for the typed write path; `dont_know` is the fallback's in-place fields
    (`AnswerChange` says why two shapes).
    `still_true`: current again, keeping the fact's own decision when it is permanent or a date still ahead
    (`confirmed_expiry`), else for the window a silence would give, counted from now.
    `ended`: archived; a standing fact also gets its `ended_text` as a history fact, a dated plan (`is_dated_plan`) is
    archived only. `never`: invalidated, nothing new. `dont_know`: the question's fallback now, as the owner's
    (`fallback_fields`). The trail reasons are the domain builders'."""
    if kind is AnswerKind.DONT_KNOW:
        return AnswerChange(fields=fallback_fields(fact, question, now, "owner"))
    update: ParsedFactUpdate
    history: Optional[str] = None
    if kind is AnswerKind.STILL_TRUE:
        update = ParsedFactUpdate(state=FactState.CURRENT, expiry=confirmed_expiry(fact, now, tz))
    elif kind is AnswerKind.ENDED:
        update = ARCHIVE_ONLY
        history = None if is_dated_plan(fact, question) else ended_text(fact.text, now, tz)
    else:
        update = ParsedFactUpdate(state=FactState.INVALIDATED)
    return AnswerChange(
        update=update, reason=answer_reason(kind, question.question_id, source), evidence_at=now, history=history
    )

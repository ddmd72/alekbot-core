"""The owner's answers to a memory-review question: which digit means what, and how each answer changes its fact.

Digits only: there is no word parsing in code. An answer in words goes to the review model as text. Each question kind
has its own options (`fact_review_options`, P2-3); an answer that is not one of its question's options is no answer."""
from datetime import datetime
from types import MappingProxyType
from typing import Final, Mapping, Optional, Tuple

from .entities import ContextPriority, FactEntity, FactState, TemporalClass
from .fact_expiry import EXPIRY_NONE
from .fact_lifecycle import compute_expiry, lifecycle_event, walked_to_unseen_text, with_event
from .fact_review_answer_change import AnswerChange
from .fact_review_answer_kind import HISTORY_ANSWERS, AnswerKind
from .fact_review_options import OPTIONS, options_of, text_key
from .fact_review_question import UNKNOWN_TEXT, AnswerSource, FactReviewQuestion, QuestionKind
from .fact_review_reasons import FallbackActor, answer_reason, fallback_reason, history_reason
from .fact_review_rules import (
    archived_only, confirmed_expiry, fallback_fields, fallback_sets_aside, fallback_writes_history,
)
from .fact_update import ParsedFactUpdate
from .fact_write_context import FactWriteContext, as_utc, reviews_stored_memory
from .interactive_message import InteractiveMessage
from .local_time import local_moment
from .new_fact_attributes import NewFactAttributes

__all__ = [
    "ARCHIVE_ONLY", "DECIDING_ANSWERS", "answer_closes_fact", "answer_fits", "answer_updates", "deciding_answer",
    "digit_answer", "ended_text", "history_fact", "history_fallback", "history_moved_refusal", "history_refusal",
    "history_trail", "option_answer", "written_text",
]

# The one update a history fact goes with: the fact archived, nothing else changed.
ARCHIVE_ONLY: Final[ParsedFactUpdate] = ParsedFactUpdate(state=FactState.ARCHIVED)

# The answers that say what is true of the fact, by question kind; `dont_know` only hands it to the question's fallback.
DECIDING_ANSWERS: Final[Mapping[QuestionKind, Tuple[AnswerKind, ...]]] = MappingProxyType({
    kind: tuple(answer for answer in options if answer is not AnswerKind.DONT_KNOW) for kind, options in OPTIONS.items()
})
_ANY_DECIDING: Final[frozenset[AnswerKind]] = frozenset(a for answers in DECIDING_ANSWERS.values() for a in answers)

# English whatever the process locale: the text is written into a stored fact.
_MONTHS: Final[tuple[str, ...]] = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def digit_answer(message: InteractiveMessage, text: str) -> Optional[AnswerKind]:
    """The answer a reply that is only a digit gives, or None (anything else is words, which are the review's).

    The digit is `message.option_for_reply`'s, which owns the digit-to-option rule and the punctuation around a lone
    digit; the option's id is the answer of the same name. A message with an option that is no answer is a bug."""
    option_id = message.option_for_reply(text)
    return None if option_id is None else AnswerKind(option_id)


def deciding_answer(raw: object, question: Optional[FactReviewQuestion] = None) -> Optional[AnswerKind]:
    """The deciding answer `raw` names, exactly, or None for anything else (never `dont_know`). With `question`, one of
    its kind's deciding answers; without, any kind's, so a request about a question that is not there still reaches the
    apply step and is told it is not found."""
    if not isinstance(raw, str):
        return None
    try:
        kind = AnswerKind(raw)
    except ValueError:
        return None
    allowed = DECIDING_ANSWERS[question.question] if question is not None else _ANY_DECIDING
    return kind if kind in allowed else None


def answer_fits(kind: AnswerKind, question: FactReviewQuestion) -> bool:
    """Whether `kind` is one of `question`'s options (`options_of`): a click from another message, a stale Cabinet page
    or a digest question's `dont_know` is not."""
    return kind in options_of(question)


def option_id(kind: AnswerKind) -> str:
    """The id of the option that stands for the answer `kind` (what `option_answer` reads back)."""
    return kind.value


def option_answer(option_id: str) -> Optional[AnswerKind]:
    """The answer an option of the question's message stands for (the option's id is the answer of the same name), or None
    for an id the message does not offer: a callback from a message of another version, or a damaged one."""
    try:
        return AnswerKind(option_id)
    except ValueError:
        return None


def written_text(question: FactReviewQuestion, key: str, fact_text: str, at: datetime, tz: str) -> str:
    """The text an outcome of `question` writes as history (`key`: `text_key` of the answer). A `review` question's is
    the one the review wrote at ask time (ValueError when it has none under `key`). A `kept_twice` question's is built by
    code from the stored `fact_text`, whole and with the month in English of `at` in the owner's zone `tz`, for
    `happened`, `not_happened` and `unknown` only (ValueError otherwise)."""
    if question.origin == "review":
        if key not in question.texts:
            raise ValueError(f"question {question.question_id} has no {key} text")
        return question.texts[key]
    said = local_moment(at, tz)
    month = f"{_MONTHS[said.month - 1]} {said.year}"
    if key == "happened":
        return f"Took place as planned (the owner confirmed in {month}): {fact_text}"
    if key == "not_happened":
        return f"Did not take place (the owner said so in {month}): {fact_text}"
    if key == UNKNOWN_TEXT:
        return f"Planned; whether it took place is unknown (no answer by {month}): {fact_text}"
    raise ValueError(f"a kept-twice question has no {key} text")


def history_fallback(
    fact: FactEntity, question: FactReviewQuestion, now: datetime, tz: str, actor: FallbackActor
) -> AnswerChange:
    """The `history` fallback of `question` (silence, or the owner's "don't remember"): its `unknown` text written as a
    history rewrite of `fact`, through the typed write path, with `fallback_reason`. ValueError for another fallback."""
    if not fallback_writes_history(question):
        raise ValueError(f"question {question.question_id} falls back to {question.fallback}, not history")
    update = ParsedFactUpdate(
        state=FactState.CURRENT,
        content=written_text(question, UNKNOWN_TEXT, fact.text, now, tz),
        expiry=EXPIRY_NONE,
        as_history=True,
    )
    return AnswerChange(
        update=update, reason=fallback_reason(question.question_id, question.fallback, actor), evidence_at=now
    )


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
    do; `dont_know` does when the question's fallback is to archive. Every other answer keeps the lineage current (a
    history is a current version). What the digest tells him."""
    if kind is AnswerKind.DONT_KNOW:
        return fallback_sets_aside(question)
    return kind in (AnswerKind.ENDED, AnswerKind.NEVER)


def answer_updates(
    kind: AnswerKind, fact: FactEntity, question: FactReviewQuestion, now: datetime, tz: str, source: AnswerSource
) -> AnswerChange:
    """What the owner's answer does to the fact he was asked about (`source`: where the answer came from).

    The update is typed (`ParsedFactUpdate`), for the typed write path; `dont_know` is the fallback's in-place fields
    (`AnswerChange` says why two shapes).
    `still_true`: current again, keeping the fact's own decision when it is permanent or a date still ahead
    (`confirmed_expiry`), else for the window a silence would give, counted from now.
    `ended`: archived; a standing fact also gets its `ended_text` as a history fact, a dated plan or noise
    (`archived_only`) is archived only. `never`: invalidated, nothing new. `dont_know`: the question's fallback now, as the owner's
    (`history_fallback` for `history`, else `fallback_fields`). P2-3: `happened`, `not_happened`, `not_needed`, `paid`
    and `not_paid` write their text (`written_text`) as history; `renewed` rewrites the fact to its text, current, with
    the window a confirmation gives (`confirmed_expiry`); `not_renewed` rewrites it to its text with expiry `none`. The
    trail reasons are the domain builders'. ValueError for an answer that is not one of the question's options."""
    if not answer_fits(kind, question):
        raise ValueError(f"{kind.value} is not an option of a {question.question} question")
    if kind is AnswerKind.DONT_KNOW:
        if fallback_writes_history(question):
            return history_fallback(fact, question, now, tz, "owner")
        return AnswerChange(fields=fallback_fields(fact, question, now, "owner"))
    update: ParsedFactUpdate
    history: Optional[str] = None
    if kind is AnswerKind.STILL_TRUE:
        update = ParsedFactUpdate(state=FactState.CURRENT, expiry=confirmed_expiry(fact, now, tz))
    elif kind is AnswerKind.ENDED:
        update = ARCHIVE_ONLY
        history = None if archived_only(fact, question) else ended_text(fact.text, now, tz)
    elif kind in HISTORY_ANSWERS:
        update = ParsedFactUpdate(
            state=FactState.CURRENT, content=written_text(question, text_key(kind), fact.text, now, tz),
            expiry=EXPIRY_NONE, as_history=True,
        )
    elif kind is AnswerKind.RENEWED:
        update = ParsedFactUpdate(
            state=FactState.CURRENT, content=written_text(question, "renewed", fact.text, now, tz),
            expiry=confirmed_expiry(fact, now, tz),
        )
    elif kind is AnswerKind.NOT_RENEWED:
        update = ParsedFactUpdate(
            state=FactState.CURRENT, content=written_text(question, "not_renewed", fact.text, now, tz),
            expiry=EXPIRY_NONE,
        )
    else:
        update = ParsedFactUpdate(state=FactState.INVALIDATED)
    return AnswerChange(
        update=update, reason=answer_reason(kind, question.question_id, source), evidence_at=now, history=history
    )

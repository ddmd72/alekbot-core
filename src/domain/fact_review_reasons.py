"""The trail reasons of the memory review: one builder per reason, and the readers that recognise them again.

A reason is code-built text in a fact's `lifecycle_events`. Two rules read them back (the revive alert and the window a
silence doubles), so the writers and the readers live here and nowhere else; no caller spells a reason."""
from datetime import date
from typing import Final, Literal, Optional

from .fact_review_answers import AnswerKind
from .fact_review_basis import ReviewAction, ReviewBasis
from .fact_review_question import AnswerSource, QuestionFallback

# Who applies a fallback: nobody answered (timeout), or the owner said he does not remember.
FallbackActor = Literal["timeout", "owner"]

_VERBS: Final[dict[str, str]] = {
    "revive": "revived", "rewrite": "rewritten", "archive": "archived", "invalidate": "invalidated",
}
_REVIVED: Final[str] = "revived on "
_REWRITTEN: Final[str] = "rewritten on "
_CONFIRMED: Final[str] = "confirmed q="
_NO_ANSWER: Final[str] = "no answer to q="
_DID_NOT_REMEMBER: Final[str] = "did not remember q="
_ANSWER_VERBS: Final[dict[AnswerKind, str]] = {
    AnswerKind.STILL_TRUE: _CONFIRMED, AnswerKind.ENDED: "ended q=", AnswerKind.NEVER: "never q=",
}


def basis_reason(action: ReviewAction, basis: ReviewBasis, plan_date: Optional[date] = None) -> str:
    """The reason of a review write that rests on `basis`: `revived on <id8>`, `rewritten on reply q=<id>`,
    `archived: date passed <date>` (`plan_date` is the plan's date). Supersede has its own reason in S3."""
    verb = _VERBS.get(action)
    if verb is None:
        raise ValueError(f"a {action} has no basis reason")
    if basis.kind == "evidence":
        if not basis.evidence_fact_id:
            raise ValueError("an evidence basis names a fact")
        return f"{verb} on {basis.evidence_fact_id[:8]}"
    if basis.kind == "reply":
        if not basis.question_id:
            raise ValueError("a reply basis names a question")
        return f"{verb} on reply q={basis.question_id}"
    if action != "archive" or plan_date is None:
        raise ValueError("a date_passed basis backs an archive and needs the plan's date")
    return f"archived: date passed {plan_date.isoformat()}"


def queued_reason(question_id: str) -> str:
    """The fact was made current while its question waits."""
    return f"queued for the owner q={question_id}"


def kept_twice_reason(question_id: str) -> str:
    """The fact was left undecided twice and queued by code."""
    return f"kept twice: queued q={question_id}"


def answer_reason(kind: AnswerKind, question_id: str, source: Optional[AnswerSource] = None) -> str:
    """The owner's answer: `confirmed q=<id> (digit)`, `ended q=<id> (cabinet)`, `never q=<id> (button)`,
    `did not remember q=<id>` (no source: the fallback decides)."""
    if kind is AnswerKind.DONT_KNOW:
        return f"{_DID_NOT_REMEMBER}{question_id}"
    if source is None:
        raise ValueError("an answer other than 'I do not remember' has a source")
    return f"{_ANSWER_VERBS[kind]}{question_id} ({source})"


def fallback_reason(question_id: str, fallback: QuestionFallback, actor: FallbackActor) -> str:
    """The fallback of a question: `no answer to q=<id>: revive|archive`, or the owner's `did not remember q=<id>`."""
    if actor == "owner":
        return answer_reason(AnswerKind.DONT_KNOW, question_id)
    return f"{_NO_ANSWER}{question_id}: {fallback}"


def is_revive_reason(reason: object) -> bool:
    """The reason of a fact coming back to current: a review revive, the owner's yes, a fallback revive, or 'I do not
    remember' (the caller also requires the entry to lead to `current`). Queueing and rewriting are not revives."""
    if not isinstance(reason, str):
        return False
    return (
        reason.startswith((_REVIVED, _CONFIRMED, _DID_NOT_REMEMBER))
        or (reason.startswith(_NO_ANSWER) and reason.endswith(": revive"))
    )


def gives_window_reason(reason: object) -> bool:
    """A revive or a rewrite: both give the fact a horizon from that moment, so the next silence doubles it."""
    return is_revive_reason(reason) or (isinstance(reason, str) and reason.startswith(_REWRITTEN))

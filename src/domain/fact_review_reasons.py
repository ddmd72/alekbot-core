"""The trail reasons of the memory review: one builder per reason, and the readers that recognise them again.

A reason is code-built text in a fact's `lifecycle_events`. Two rules read them back (the revive alert and the window a
silence doubles), so the writers and the readers live here and nowhere else; no caller spells a reason."""
import dataclasses
from datetime import date, datetime
from typing import Final, Literal, Optional

from .entities import FactEntity
from .fact_review_answer_kind import HISTORY_ANSWERS, AnswerKind
from .fact_review_basis import ReviewAction, ReviewBasis
from .fact_review_question import AnswerSource, FactReviewQuestion, QuestionFallback

# Who applies a fallback: nobody answered (timeout), or the owner said he does not remember.
FallbackActor = Literal["timeout", "owner"]

_VERBS: Final[dict[str, str]] = {
    "revive": "revived", "rewrite": "rewritten", "archive": "archived", "invalidate": "invalidated",
}
_REVIVED: Final[str] = "revived on "
_REWRITTEN: Final[str] = "rewritten on "
_AS_HISTORY: Final[str] = "rewritten as history"
_REWRITTEN_Q: Final[str] = "rewritten q="
_RENEWED: Final[str] = "renewed q="
_CONFIRMED: Final[str] = "confirmed q="
_NO_ANSWER: Final[str] = "no answer to q="
_DID_NOT_REMEMBER: Final[str] = "did not remember q="
_ANSWER_VERBS: Final[dict[AnswerKind, str]] = {
    AnswerKind.STILL_TRUE: _CONFIRMED, AnswerKind.ENDED: "ended q=", AnswerKind.NEVER: "never q=",
}


def basis_reason(
    action: ReviewAction, basis: ReviewBasis, plan_date: Optional[date] = None, *, as_history: bool = False
) -> str:
    """The reason of a review write that rests on `basis`: `revived on <id8>`, `rewritten on reply q=<id>`,
    `archived: date passed <date>` (`plan_date` is the plan's date). Supersede has its own reason in S3.

    `as_history` (only with a rewrite, P2-1): `rewritten as history on <id8>`, `rewritten as history on reply q=<id>`,
    `rewritten as history: date passed <date>`."""
    if as_history:
        return _history_basis_reason(action, basis, plan_date)
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


def _history_basis_reason(action: ReviewAction, basis: ReviewBasis, plan_date: Optional[date]) -> str:
    if action != "rewrite":
        raise ValueError(f"as_history goes with a rewrite, not a {action}")
    if basis.kind == "evidence":
        if not basis.evidence_fact_id:
            raise ValueError("an evidence basis names a fact")
        return f"{_AS_HISTORY} on {basis.evidence_fact_id[:8]}"
    if basis.kind == "reply":
        if not basis.question_id:
            raise ValueError("a reply basis names a question")
        return f"{_AS_HISTORY} on reply q={basis.question_id}"
    if plan_date is None:
        raise ValueError("a date_passed basis needs the plan's date")
    return f"{_AS_HISTORY}: date passed {plan_date.isoformat()}"


def review_reason(
    action: ReviewAction,
    basis: Optional[ReviewBasis],
    named: Optional[FactEntity],
    superseded_by: Optional[str],
    plan_date: Optional[date] = None,
    *,
    as_history: bool = False,
) -> Optional[str]:
    """The trail reason of a review write the basis rules let through (plan §3.5): a supersede names its successor, any
    other action its basis (`basis_reason`; `plan_date` for `date_passed`; `as_history` for a history rewrite). `named`
    is the fact the evidence or the successor id was read as; the reason cites that document, the one the decision rests
    on. None without a basis."""
    if action == "supersede":
        return superseded_reason(named.id if named is not None else str(superseded_by))
    if basis is None:
        return None
    if basis.kind == "evidence" and named is not None:
        basis = dataclasses.replace(basis, evidence_fact_id=named.id)
    return basis_reason(action, basis, plan_date, as_history=as_history)


def evidence_moment(
    basis: Optional[ReviewBasis], named: Optional[FactEntity], question: Optional[FactReviewQuestion]
) -> Optional[datetime]:
    """When what a review write rests on was said (plan §3.5): the evidence fact's `reported_date`, or the owner's reply's
    `reply_at`; None for `date_passed`, no basis, or a fact or question that is not there."""
    if basis is None:
        return None
    if basis.kind == "evidence":
        return named.reported_date if named is not None else None
    if basis.kind == "reply":
        return question.reply_at if question is not None else None
    return None


def superseded_reason(successor_id: str) -> str:
    """A fact closed in favour of `successor_id` (S3's text, also the review's and the owner's supersede)."""
    return f"superseded by {successor_id}"


def history_reason(archived_id: str) -> str:
    """A history fact born beside the archive of `archived_id` (the owner's "ended" on a standing fact, S4-D27)."""
    return f"history of {archived_id}"


def queued_reason(question_id: str) -> str:
    """The fact was made current while its question waits."""
    return f"queued for the owner q={question_id}"


def kept_twice_reason(question_id: str) -> str:
    """The fact was left undecided twice and queued by code."""
    return f"kept twice: queued q={question_id}"


def answer_reason(kind: AnswerKind, question_id: str, source: Optional[AnswerSource] = None) -> str:
    """The owner's answer: `confirmed q=<id> (digit)`, `ended q=<id> (cabinet)`, `never q=<id> (button)`,
    `did not remember q=<id>` (no source: the fallback decides). P2-3: a history answer is
    `rewritten as history q=<id>: <answer> (<source>)`, `renewed` is `renewed q=<id> (<source>)` and `not_renewed` is
    `rewritten q=<id>: not_renewed (<source>)`."""
    if kind is AnswerKind.DONT_KNOW:
        return f"{_DID_NOT_REMEMBER}{question_id}"
    if source is None:
        raise ValueError("an answer other than 'I do not remember' has a source")
    if kind in HISTORY_ANSWERS:
        return f"{_AS_HISTORY} q={question_id}: {kind.value} ({source})"
    if kind is AnswerKind.RENEWED:
        return f"{_RENEWED}{question_id} ({source})"
    if kind is AnswerKind.NOT_RENEWED:
        return f"{_REWRITTEN_Q}{question_id}: {kind.value} ({source})"
    return f"{_ANSWER_VERBS[kind]}{question_id} ({source})"


def fallback_reason(question_id: str, fallback: QuestionFallback, actor: FallbackActor) -> str:
    """The fallback of a question: `no answer to q=<id>: revive|archive`, or the owner's `did not remember q=<id>`. A
    `history` fallback is a rewrite: `rewritten as history: no answer to q=<id>`, or
    `rewritten as history: did not remember q=<id>`."""
    if fallback == "history":
        said = _DID_NOT_REMEMBER if actor == "owner" else _NO_ANSWER
        return f"{_AS_HISTORY}: {said}{question_id}"
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


def is_rewrite_reason(reason: object) -> bool:
    """The reason of a rewrite (`rewritten on …`, `rewritten as history…`, the owner's `rewritten q=…` and `renewed q=…`):
    a new version, not a revive."""
    return isinstance(reason, str) and reason.startswith((_REWRITTEN, _AS_HISTORY, _REWRITTEN_Q, _RENEWED))


def gives_window_reason(reason: object) -> bool:
    """A revive or a rewrite: both give the fact a horizon from that moment, so the next silence doubles it."""
    return is_revive_reason(reason) or is_rewrite_reason(reason)

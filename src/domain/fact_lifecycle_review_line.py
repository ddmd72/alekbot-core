"""The memory review's part of the janitor's ops line (Facts S4 plan §3.11): the segments an account row gains from
yesterday's review counters and the run's own question figures. Pure: the service passes the numbers, this words them."""
from dataclasses import dataclass
from typing import Final, Iterable, List, Tuple

from .fact_lifecycle_day import CounterField, FactLifecycleDay
from .fact_review_question import FactReviewQuestion, QuestionStatus

# The review bucket list is not a breakdown of `reviewed`: `kept` also counts a timed-out candidate the model left
# undecided (it is in `timed out` too), and `queued` also counts a kept-twice question, which is not in the batch.
# Each figure is a counter of its own, and the wording says so.
REVIEW_HEAD: Final[str] = "review: reviewed {reviewed}; counted on their own: "

_OWN_BUCKETS: Final[Tuple[Tuple[str, CounterField], ...]] = (
    ("revived", CounterField.REVIVED),
    ("rewritten", CounterField.REWRITTEN),
    ("archived", CounterField.ARCHIVED_BY_REVIEW),
    ("invalidated", CounterField.INVALIDATED),
    ("superseded", CounterField.SUPERSEDED),
    ("kept", CounterField.KEPT),
    ("queued", CounterField.QUEUED),
    ("to history", CounterField.TO_HISTORY),
    ("folded", CounterField.FOLDED),
)
# Counters of the questions; `timed out` is one of them, not part of the review buckets.
_QUESTION_COUNTERS: Final[Tuple[Tuple[str, CounterField], ...]] = (
    ("questions sent", CounterField.QUESTIONS_SENT),
    ("answered by digit", CounterField.ANSWERED_BY_DIGIT),
    ("by button", CounterField.ANSWERED_BY_BUTTON),
    ("in the Cabinet", CounterField.ANSWERED_IN_CABINET),
    ("by reply", CounterField.CLOSED_BY_REPLY),
    ("timed out", CounterField.TIMED_OUT),
)
_FAILURES: Final[Tuple[Tuple[str, CounterField], ...]] = (
    ("review failed", CounterField.REVIEW_FAILED),
    ("refusals", CounterField.REVIEW_REFUSALS),
    ("fallbacks not applied", CounterField.FALLBACKS_NOT_APPLIED),
)
_UNRECORDED: Final[Tuple[Tuple[str, CounterField], ...]] = (("replies not recorded", CounterField.REPLY_INTAKE_FAILED),)

# The counters the line prints, in its order. The remaining `REVIEW_FIGURES` are stored and have no place on it.
LINE_COUNTERS: Final[Tuple[CounterField, ...]] = tuple(
    counter for group in (
        ((("reviewed", CounterField.REVIEWED),)), _OWN_BUCKETS, _QUESTION_COUNTERS, _FAILURES, _UNRECORDED
    ) for _, counter in group
)


@dataclass(frozen=True)
class QuestionStanding:
    """What the review's queue held when the run began: the questions waiting to be sent and the ones sent and
    unanswered. A figure of the run, not a counter."""

    queued: int = 0
    unanswered: int = 0


def question_standing(questions: Iterable[FactReviewQuestion]) -> QuestionStanding:
    """The standing of an account's open chat questions (a digest question is never sent, so never in the queue)."""
    held = [q for q in questions if q.in_chat]
    return QuestionStanding(
        queued=sum(1 for q in held if q.status is QuestionStatus.QUEUED),
        unanswered=sum(1 for q in held if q.status is QuestionStatus.UNANSWERED),
    )


def has_review_line(row: FactLifecycleDay, due_waiting: int, standing: QuestionStanding) -> bool:
    """Any figure the line prints for the review is above zero (`review_enqueued` alone does not earn a line)."""
    return any(getattr(row, counter.value) for counter in LINE_COUNTERS) or any(
        (due_waiting, standing.queued, standing.unanswered)
    )


def review_segments(row: FactLifecycleDay, due_waiting: int, standing: QuestionStanding) -> str:
    """The review's segments of an account row, each ` | …` and each only when one of its figures is above zero, then
    ` | enqueued 0|1` when any segment is there. Empty when the review has nothing to say. The counters are the row's
    (the service gives yesterday's); `due_waiting` and `standing` are the run's."""
    parts: List[str] = []
    own = [f"{label} {getattr(row, counter.value)}" for label, counter in _OWN_BUCKETS]
    if row.reviewed or any(getattr(row, counter.value) for _, counter in _OWN_BUCKETS):
        parts.append(REVIEW_HEAD.format(reviewed=row.reviewed) + ", ".join(own))
    questions = [f"{label} {getattr(row, counter.value)}" for label, counter in _QUESTION_COUNTERS]
    questions += [f"queue {standing.queued}", f"unanswered {standing.unanswered}", f"due_waiting {due_waiting}"]
    if any(getattr(row, counter.value) for _, counter in _QUESTION_COUNTERS) or any(
        (standing.queued, standing.unanswered, due_waiting)
    ):
        parts.append(", ".join(questions))
    for group in (_FAILURES, _UNRECORDED):
        if any(getattr(row, counter.value) for _, counter in group):
            parts.append(", ".join(f"{label} {getattr(row, counter.value)}" for label, counter in group))
    if not parts:
        return ""
    parts.append(f"enqueued {row.review_enqueued}")
    return "".join(f" | {part}" for part in parts)

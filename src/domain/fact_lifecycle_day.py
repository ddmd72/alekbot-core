"""One account's fact-lifecycle numbers for one UTC day: the janitor's snapshot of its run and the event counters."""
from dataclasses import dataclass, fields
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Dict, Final, Iterable, Mapping, Optional, Tuple

from .local_time import local_date


class CounterField(StrEnum):
    """A number added to by whoever meets its event; a janitor snapshot never replaces one."""

    EXPIRY_TOOL_ERRORS = "expiry_tool_errors"
    REVIEWED = "reviewed"
    REVIVED = "revived"
    REWRITTEN = "rewritten"
    ARCHIVED_BY_REVIEW = "archived_by_review"
    INVALIDATED = "invalidated"
    SUPERSEDED = "superseded"
    KEPT = "kept"
    QUEUED = "queued"
    QUESTIONS_SENT = "questions_sent"
    NOT_DELIVERED = "not_delivered"
    ANSWERED_BY_DIGIT = "answered_by_digit"
    ANSWERED_BY_BUTTON = "answered_by_button"
    ANSWERED_IN_CABINET = "answered_in_cabinet"
    ANSWERS_NOT_APPLIED = "answers_not_applied"
    CLOSED_BY_REPLY = "closed_by_reply"
    TIMED_OUT = "timed_out"
    REVIEW_FAILED = "review_failed"
    REVIEW_REFUSALS = "review_refusals"
    REPLIES_IN_WORDS = "replies_in_words"
    ALREADY_ANSWERED = "already_answered"
    REPLY_INTAKE_FAILED = "reply_intake_failed"
    LABELS_FALLBACK = "labels_fallback"
    TO_HISTORY = "to_history"
    FOLDED = "folded"
    FALLBACKS_NOT_APPLIED = "fallbacks_not_applied"


# The counters of the memory review and its answers: every counter but the expiry tool errors, which the expiry path counts.
# Listed, not derived: the ops line prints the first twenty in this order (`fact_lifecycle_review_line`), the last five
# are counted and stored but have no place on it. A new counter is added here by hand, and a test refuses one that is missing.
REVIEW_FIGURES: Final[Tuple[CounterField, ...]] = (
    CounterField.REVIEWED,
    CounterField.REVIVED,
    CounterField.REWRITTEN,
    CounterField.ARCHIVED_BY_REVIEW,
    CounterField.INVALIDATED,
    CounterField.SUPERSEDED,
    CounterField.KEPT,
    CounterField.QUEUED,
    CounterField.TO_HISTORY,
    CounterField.FOLDED,
    CounterField.QUESTIONS_SENT,
    CounterField.ANSWERED_BY_DIGIT,
    CounterField.ANSWERED_BY_BUTTON,
    CounterField.ANSWERED_IN_CABINET,
    CounterField.CLOSED_BY_REPLY,
    CounterField.TIMED_OUT,
    CounterField.REVIEW_FAILED,
    CounterField.REVIEW_REFUSALS,
    CounterField.FALLBACKS_NOT_APPLIED,
    CounterField.REPLY_INTAKE_FAILED,
    CounterField.NOT_DELIVERED,
    CounterField.ANSWERS_NOT_APPLIED,
    CounterField.REPLIES_IN_WORDS,
    CounterField.ALREADY_ANSWERED,
    CounterField.LABELS_FALLBACK,
)


def counter_day(at: datetime) -> date:
    """The day a counter is added to: the UTC date of the event (a naive time is UTC)."""
    return local_date(at, "UTC")


def check_count(name: str, value: Any) -> None:
    """Raise ValueError unless `value` is a count: a non-negative int (a bool is not one)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class SweepSnapshot:
    """The janitor's numbers for one account's run. The last run of the day replaces the earlier one whole.
    `oldest_stale_days` is None when the account has no stale fact."""

    run_at: datetime
    due: int
    newly_due: int
    no_decision: int
    staled: int
    would_stale: int
    deferred: int
    stale_conflicts: int
    breaker: str
    backlog: int
    oldest_stale_days: Optional[int]
    review_enqueued: int

    def __post_init__(self) -> None:
        if not isinstance(self.run_at, datetime):
            raise ValueError("run_at must be a datetime")
        if not isinstance(self.breaker, str):
            raise ValueError("breaker must be a string")
        for name in _SNAPSHOT_COUNTS:
            check_count(name, getattr(self, name))
        if self.oldest_stale_days is not None:
            check_count("oldest_stale_days", self.oldest_stale_days)


_SNAPSHOT_COUNTS = (
    "due", "newly_due", "no_decision", "staled", "would_stale", "deferred", "stale_conflicts", "backlog",
    "review_enqueued",
)


@dataclass(frozen=True)
class FactLifecycleDay:
    """One account's lifecycle numbers for one UTC day: the janitor's last run (none yet: `run_at` is None) and the
    counters. A number nobody wrote is 0; `of` builds a row from a run's snapshot."""

    account_id: str
    day: date
    run_at: Optional[datetime] = None
    due: int = 0
    newly_due: int = 0
    no_decision: int = 0
    staled: int = 0
    would_stale: int = 0
    deferred: int = 0
    stale_conflicts: int = 0
    breaker: str = ""
    backlog: int = 0
    oldest_stale_days: Optional[int] = None
    review_enqueued: int = 0
    expiry_tool_errors: int = 0
    reviewed: int = 0
    revived: int = 0
    rewritten: int = 0
    archived_by_review: int = 0
    invalidated: int = 0
    superseded: int = 0
    kept: int = 0
    queued: int = 0
    questions_sent: int = 0
    not_delivered: int = 0
    answered_by_digit: int = 0
    answered_by_button: int = 0
    answered_in_cabinet: int = 0
    answers_not_applied: int = 0
    closed_by_reply: int = 0
    timed_out: int = 0
    review_failed: int = 0
    review_refusals: int = 0
    replies_in_words: int = 0
    already_answered: int = 0
    reply_intake_failed: int = 0
    labels_fallback: int = 0
    to_history: int = 0
    folded: int = 0
    fallbacks_not_applied: int = 0

    def counts(self, figures: Iterable[CounterField]) -> Dict[CounterField, int]:
        """This row's value of each of `figures`."""
        return {figure: getattr(self, figure.value) for figure in figures}

    def has_review_figures(self) -> bool:
        """Any counter of the memory review or its answers is above zero (`REVIEW_FIGURES`)."""
        return any(getattr(self, figure.value) for figure in REVIEW_FIGURES)

    @classmethod
    def of(
        cls,
        account_id: str,
        day: date,
        snapshot: Optional[SweepSnapshot],
        counts: Mapping[CounterField, int],
    ) -> "FactLifecycleDay":
        """The day's row from the run's snapshot (None: no run that day) and the counters (one not given is 0)."""
        values: Dict[str, Any] = (
            {} if snapshot is None else {f.name: getattr(snapshot, f.name) for f in fields(SweepSnapshot)}
        )
        values.update({CounterField(name).value: count for name, count in counts.items()})
        return cls(account_id=account_id, day=day, **values)

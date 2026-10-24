"""What a consolidation pass tells a fact write about itself, and the `reported_date` rules that follow from it."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .entities import ExpiryKind, FactState
from .fact_expiry import Expiry
from .fact_lifecycle import MIN_REVIEW_HORIZON_DAYS, born_due, compute_expiry, horizon_from_now, short_review_horizon
from .local_time import local_date, local_moment
from .pass_actor import PassActor


@dataclass(frozen=True)
class FactWriteContext:
    """The pass a fact write belongs to. The caller builds it once per pass; there is no default and no ambient copy.

    `actor`: the pass (`PassActor`; a label that is no pass is refused here, a plain `str` of a pass is converted). Every
    rule that depends on the pass (`model_dates`, `accepts`, `floors_due_horizon`) reads the actor's row, so a context
    cannot disagree with the table. `span`: (first, last) time the owner spoke in this batch, aware.
    """

    actor: PassActor
    timezone: str
    span: Optional[Tuple[datetime, datetime]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "actor", PassActor(self.actor))
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, KeyError, ValueError, OSError):
            raise ValueError(f"unknown timezone {self.timezone!r}") from None
        if self.span is not None:
            first, last = self.span
            if first.tzinfo is None or last.tzinfo is None:
                raise ValueError("a write context span must be timezone-aware; convert with as_utc first")
            if first > last:
                raise ValueError("a write context span must run from the earlier to the later time")

    @classmethod
    def for_pass(cls, actor: PassActor, timezone: str, span: Optional[Tuple[datetime, datetime]] = None) -> "FactWriteContext":
        """The context of a pass."""
        return cls(actor=actor, timezone=timezone, span=span)

    @property
    def model_dates(self) -> bool:
        """True = a model `reported_date` is clamped into the span and used (the conversation passes); False = ignored."""
        return self.actor.model_dates

    @property
    def accepts(self) -> FrozenSet[FactState]:
        """The states the write's own target may be in."""
        return self.actor.accepts

    @property
    def floors_due_horizon(self) -> bool:
        """Whether a horizon that resolves within a day is floored to two days from now."""
        return self.actor.floors_due_horizon


def as_utc(value: datetime) -> datetime:
    """`value` as an aware UTC datetime; a naive time is UTC. Raises TypeError for anything that is not a datetime."""
    if not isinstance(value, datetime):
        raise TypeError(f"as_utc takes a datetime, not {type(value).__name__}")
    return local_moment(value, "UTC")


def decide_expiry(
    decision: Expiry, reported: datetime, now: datetime, context: FactWriteContext
) -> Tuple[Dict[str, Any], Optional[str]]:
    """compute_expiry, then the born-due rule of this pass. The one place a decision becomes stored fields.

    Lives here, not in `fact_lifecycle`: the context's actor table imports `fact_lifecycle`, so the reverse import is a cycle.
    Raises FactExpiryError (FactBornDueError) when the decision is due and this pass does not floor it.
    """
    return born_due(compute_expiry(decision, reported, context.timezone), reported, now, context.timezone, context.actor)


def reviews_stored_memory(context: FactWriteContext) -> bool:
    """The review and the owner's answers: the passes whose row reaches a stale fact (S4-D5, plan §3.6)."""
    # Derived from `accepts` (the card's rule): a future row that accepts STALE without the review's write rules needs
    # its own column in the PassActor table instead.
    return FactState.STALE in context.accepts


def review_horizon(
    decision: Expiry, reported: datetime, now: datetime, context: FactWriteContext
) -> Tuple[Dict[str, Any], Optional[str]]:
    """The stored fields of a review or owner revive or rewrite (S4-D5): a horizon counted from the later of `reported`
    (the date after the basis rule) and `now`, stored from `reported`; a `date` or `none` as `compute_expiry` stores it.
    Then the born-due rule of the pass's row, which has no floor.

    Raises ValueError for a context that is no review or owner pass, FactBornDueError for a decision that is due or a
    horizon under MIN_REVIEW_HORIZON_DAYS."""
    if not reviews_stored_memory(context):
        raise ValueError(f"a review horizon belongs to the review or the owner, not to {context.actor}")
    if decision.kind is ExpiryKind.HORIZON and decision.days is not None:
        if decision.days < MIN_REVIEW_HORIZON_DAYS:
            raise short_review_horizon(decision.days)
        fields = horizon_from_now(decision.days, reported, now)
    else:
        fields = compute_expiry(decision, reported, context.timezone)
    return born_due(fields, reported, now, context.timezone, context.actor)


def span_of(times: Iterable[datetime]) -> Optional[Tuple[datetime, datetime]]:
    """(earliest, latest) of `times` as aware UTC, or None when there are none."""
    moments = [as_utc(t) for t in times]
    return (min(moments), max(moments)) if moments else None


def clamp_to_span(value: datetime, span: Optional[Tuple[datetime, datetime]], now: datetime) -> datetime:
    """`value` pulled into the span, and never later than `now`."""
    moment = as_utc(value)
    if span is not None:
        moment = min(max(moment, as_utc(span[0])), as_utc(span[1]))
    return min(moment, as_utc(now))


NO_READABLE_SOURCE_NOTE = "derived_from named no readable fact of this account; the end of the batch is used"


def kept_reported_date_note(reported: datetime, timezone: str) -> str:
    """The one line a pass whose model dates do not count gets when its call carried a `reported_date`."""
    return (
        f"reported_date is kept by the system in this pass ({local_date(reported, timezone).isoformat()}); "
        "expiry horizons count from it"
    )


REVIEW_REPORTED_DATE_NOTE = (
    "reported_date is kept by the system in this pass (it moves only on evidence or the owner's answer); "
    "expiry horizons count from now"
)


def reported_date_for_new(
    ctx: FactWriteContext,
    model_value: Optional[datetime],
    derived: Sequence[datetime],
    now: datetime,
    *,
    derived_named: int = 0,
) -> Tuple[datetime, List[str]]:
    """The `reported_date` of a created fact, and the notes for the model when something it sent was not used.

    The notes are whole and separate (none, one or two), so a reader can recognise each by its code
    (`domain.fact_tool_result`); the tool result joins them.

    `derived`: the dates of the readable `derived_from` sources, used only on a pass whose model dates do not count (the latest). `derived_named`:
    how many ids the call named, so a source that cannot be read, or a `derived_from` ignored in a conversation pass, is said.
    """
    now = as_utc(now)
    end = min(as_utc(ctx.span[1]), now) if ctx.span is not None else now
    notes: List[str] = []
    if ctx.model_dates:
        if derived_named or derived:
            notes.append("derived_from is ignored: in this pass the date comes from the conversation")
        chosen = clamp_to_span(model_value, ctx.span, now) if model_value is not None else end
        return chosen, notes
    if derived:
        chosen = max(as_utc(d) for d in derived)
    else:
        chosen = end
        if derived_named:
            notes.append(NO_READABLE_SOURCE_NOTE)
    if model_value is not None:
        notes.append(kept_reported_date_note(chosen, ctx.timezone))
    return chosen, notes


def reported_date_for_merge(
    ctx: FactWriteContext, model_value: Optional[datetime], source_dates: Sequence[datetime], now: datetime
) -> Tuple[datetime, List[str]]:
    """The `reported_date` of a merged fact, and the notes (none or one) when the model's date was not used.

    Never earlier than the newest merged fact's own date. A pass whose model dates do not count takes exactly that date
    (and says so when the call carried one). A conversation pass takes the later of that and the model's span-clamped
    date (else the span end), like an update.
    """
    now = as_utc(now)
    newest = max((as_utc(d) for d in source_dates), default=None)
    if ctx.model_dates:
        if model_value is not None:
            chosen = clamp_to_span(model_value, ctx.span, now)
        else:
            chosen = min(as_utc(ctx.span[1]), now) if ctx.span is not None else now
        return (max(chosen, newest) if newest is not None else chosen), []
    if newest is not None:
        chosen = newest
    else:
        chosen = min(as_utc(ctx.span[1]), now) if ctx.span is not None else now
    return chosen, ([kept_reported_date_note(chosen, ctx.timezone)] if model_value is not None else [])


def reported_date_for_update(
    ctx: FactWriteContext,
    model_value: Optional[datetime],
    stored: datetime,
    content_change: bool,
    now: datetime,
    evidence_at: Optional[datetime] = None,
) -> Tuple[datetime, Optional[str]]:
    """The `reported_date` after an update, and a note. It means "when the owner last stated it": it never moves back.

    The review and the owner (`reviews_stored_memory`): the later of `stored` and `evidence_at` (the evidence fact's
    date, the reply's or the answer's time), else `stored`; a date the model sent is never used and gets
    REVIEW_REPORTED_DATE_NOTE. The caller passes `evidence_at` only for a revive. Every other pass ignores `evidence_at`."""
    now = as_utc(now)
    stored = as_utc(stored)
    if reviews_stored_memory(ctx):
        chosen = max(stored, as_utc(evidence_at)) if evidence_at is not None else stored
        return chosen, (REVIEW_REPORTED_DATE_NOTE if model_value is not None else None)
    if not ctx.model_dates:
        note = kept_reported_date_note(stored, ctx.timezone) if model_value is not None else None
        return stored, note
    if model_value is not None:
        return max(stored, clamp_to_span(model_value, ctx.span, now)), None
    if content_change and ctx.span is not None:
        return max(stored, min(as_utc(ctx.span[1]), now)), None
    return stored, None


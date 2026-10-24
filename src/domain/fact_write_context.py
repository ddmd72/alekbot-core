"""What a consolidation pass tells a fact write about itself, and the `reported_date` rules that follow from it."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, FrozenSet, Iterable, Optional, Sequence, Tuple, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .entities import FactState
from .fact_expiry import Expiry
from .fact_lifecycle import born_due, compute_expiry
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


def as_utc(value: Union[float, str, datetime]) -> datetime:
    """`value` as an aware UTC datetime: epoch seconds, an ISO string or a datetime. A naive time is UTC (email dates are).

    Raises ValueError for a string that is not ISO-8601.
    """
    if isinstance(value, str):
        value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return local_moment(value, "UTC")


def decide_expiry(
    decision: Expiry, reported: datetime, now: datetime, context: FactWriteContext
) -> Tuple[Dict[str, Any], Optional[str]]:
    """compute_expiry, then the born-due rule of this pass. The one place a decision becomes stored fields.

    Lives here, not in `fact_lifecycle`: the context's actor table imports `fact_lifecycle`, so the reverse import is a cycle.
    Raises FactExpiryError (FactBornDueError) when the decision is due and this pass does not floor it.
    """
    return born_due(
        compute_expiry(decision, reported, context.timezone), reported, now, context.timezone, context.floors_due_horizon
    )


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


def reported_date_for_new(
    ctx: FactWriteContext,
    model_value: Optional[datetime],
    derived: Sequence[datetime],
    now: datetime,
    *,
    derived_named: int = 0,
) -> Tuple[datetime, Optional[str]]:
    """The `reported_date` of a created fact, and a note for the model when something it sent was not used.

    `derived`: the dates of the readable `derived_from` sources, used only on a pass whose model dates do not count (the latest). `derived_named`:
    how many ids the call named, so a source that cannot be read, or a `derived_from` ignored in a conversation pass, is said.
    """
    now = as_utc(now)
    end = min(as_utc(ctx.span[1]), now) if ctx.span is not None else now
    notes = []
    if ctx.model_dates:
        if derived_named or derived:
            notes.append("derived_from is ignored: in this pass the date comes from the conversation")
        chosen = clamp_to_span(model_value, ctx.span, now) if model_value is not None else end
        return chosen, _joined(notes)
    if derived:
        chosen = max(as_utc(d) for d in derived)
    else:
        chosen = end
        if derived_named:
            notes.append(NO_READABLE_SOURCE_NOTE)
    if model_value is not None:
        notes.append(kept_reported_date_note(chosen, ctx.timezone))
    return chosen, _joined(notes)


def reported_date_for_merge(
    ctx: FactWriteContext, model_value: Optional[datetime], source_dates: Sequence[datetime], now: datetime
) -> Tuple[datetime, Optional[str]]:
    """The `reported_date` of a merged fact, and a note when the model's date was not used.

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
        return (max(chosen, newest) if newest is not None else chosen), None
    if newest is not None:
        chosen = newest
    else:
        chosen = min(as_utc(ctx.span[1]), now) if ctx.span is not None else now
    note = kept_reported_date_note(chosen, ctx.timezone) if model_value is not None else None
    return chosen, note


def reported_date_for_update(
    ctx: FactWriteContext, model_value: Optional[datetime], stored: datetime, content_change: bool, now: datetime
) -> Tuple[datetime, Optional[str]]:
    """The `reported_date` after an update, and a note. It means "when the owner last stated it": it never moves back."""
    now = as_utc(now)
    stored = as_utc(stored)
    if not ctx.model_dates:
        note = kept_reported_date_note(stored, ctx.timezone) if model_value is not None else None
        return stored, note
    if model_value is not None:
        return max(stored, clamp_to_span(model_value, ctx.span, now)), None
    if content_change and ctx.span is not None:
        return max(stored, min(as_utc(ctx.span[1]), now)), None
    return stored, None


def _joined(notes: Sequence[str]) -> Optional[str]:
    return "; ".join(notes) if notes else None

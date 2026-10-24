"""What a consolidation pass tells a fact write about itself, and the `reported_date` rules that follow from it."""

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional, Sequence, Tuple, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .local_time import local_date, local_moment


@dataclass(frozen=True)
class FactWriteContext:
    """The pass a fact write belongs to. The caller builds it once per pass; there is no default and no ambient copy.

    `actor`: stage1 | stage2a | stage2b | backstop | email | cluster. `span`: (first, last) time the owner spoke in
    this batch, aware. `model_dates`: True = a model `reported_date` is clamped into the span and used (stage1, email);
    False = ignored (cleanup passes: the system keeps the date).
    """

    actor: str
    timezone: str
    span: Optional[Tuple[datetime, datetime]]
    model_dates: bool

    def __post_init__(self) -> None:
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


def as_utc(value: Union[float, str, datetime]) -> datetime:
    """`value` as an aware UTC datetime: epoch seconds, an ISO string or a datetime. A naive time is UTC (email dates are).

    Raises ValueError for a string that is not ISO-8601.
    """
    if isinstance(value, str):
        value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return local_moment(value, "UTC")


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
    """The one line a cleanup pass gets when its call carried a `reported_date`."""
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

    `derived`: the dates of the readable `derived_from` sources, used only on a cleanup pass (the latest). `derived_named`:
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
) -> datetime:
    """The `reported_date` of a merged fact: never earlier than the newest merged fact's own date.

    A cleanup pass takes exactly that date. A conversation pass takes the later of that and the model's span-clamped
    date (else the span end), like an update.
    """
    now = as_utc(now)
    newest = max((as_utc(d) for d in source_dates), default=None)
    if ctx.model_dates:
        if model_value is not None:
            chosen = clamp_to_span(model_value, ctx.span, now)
        else:
            chosen = min(as_utc(ctx.span[1]), now) if ctx.span is not None else now
        return max(chosen, newest) if newest is not None else chosen
    if newest is not None:
        return newest
    return min(as_utc(ctx.span[1]), now) if ctx.span is not None else now


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

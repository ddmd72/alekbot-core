"""The `expiry` argument of the fact tools: parsing it, and showing a stored decision to the model in the shape it sends."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Final, Optional, Tuple

from .entities import ExpiryKind
from .exceptions import FactExpiryError
from .local_time import local_date, local_moment

NOTHING_WRITTEN: Final[str] = "Nothing was written. Resend this call with a valid expiry before retiring any fact it replaces."
EXPIRY_IGNORED_ON_CLOSE: Final[str] = "expiry is not used when a fact is closed; ignored"
EXPIRY_SHAPES: Final[str] = "{kind: date, date: YYYY-MM-DD}, {kind: horizon, days: N} or {kind: none}"
MAX_HORIZON_DAYS: Final[int] = 36500
MIN_YEAR, MAX_YEAR = 1900, 9998  # keeps the end-of-day and horizon arithmetic inside datetime's range


@dataclass(frozen=True)
class Expiry:
    kind: ExpiryKind
    on: Optional[date] = None      # kind date: the last day the fact holds
    days: Optional[int] = None     # kind horizon: days counted from when the owner said it

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", ExpiryKind(self.kind))
        if self.kind is ExpiryKind.DATE and self.on is None:
            raise ValueError("an Expiry of kind date needs `on`")
        if self.kind is ExpiryKind.HORIZON and self.days is None:
            raise ValueError("an Expiry of kind horizon needs `days`")


EXPIRY_NONE: Final[Expiry] = Expiry(ExpiryKind.NONE)


def parse_expiry(raw: object) -> Expiry:
    """The model's `expiry` as an Expiry. Raises FactExpiryError, whose text is the fix the model reads."""
    if raw is None:
        raise FactExpiryError(f"`null` is not an answer; use {{kind: none}} for 'never expires'. {NOTHING_WRITTEN}")
    if isinstance(raw, str):
        if raw.strip().lower() == "none":
            return EXPIRY_NONE
        raise _malformed(f"{raw!r} is not an expiry")
    if not isinstance(raw, dict):
        raise _malformed(f"{type(raw).__name__} is not an expiry")
    kind_raw = raw.get("kind")
    kind = kind_raw.strip().lower() if isinstance(kind_raw, str) else None
    if kind == ExpiryKind.NONE.value:
        return EXPIRY_NONE
    if kind == ExpiryKind.DATE.value:
        return Expiry(ExpiryKind.DATE, on=_parse_day(raw.get("date")))
    if kind == ExpiryKind.HORIZON.value:
        return Expiry(ExpiryKind.HORIZON, days=_parse_days(raw.get("days")))
    raise _malformed(f"kind {kind_raw!r} is unknown")


def stored_expiry(
    kind: Optional[ExpiryKind], ttl_days: Optional[int], expires_at: Optional[datetime], timezone: str
) -> Optional[Expiry]:
    """A stored decision as an Expiry, the one a write could send to keep it (`timezone`: the owner's zone, which a date's
    last day is read in). Sent back unchanged it keeps the same `expires_at`, unless the date is today or past (refused as
    born due) or a cleanup pass floored a horizon (floored again from the new now). No kind, or a kind without its data,
    is None: there is no decision to send."""
    if kind is ExpiryKind.NONE:
        return EXPIRY_NONE
    if kind is ExpiryKind.DATE and expires_at is not None:
        # One microsecond before the next midnight is the last moment of the day, DST-safe (a day can be 23 h).
        return Expiry(ExpiryKind.DATE, on=local_date(local_moment(expires_at, "UTC") - timedelta(microseconds=1), timezone))
    if kind is ExpiryKind.HORIZON and ttl_days is not None:
        return Expiry(ExpiryKind.HORIZON, days=ttl_days)
    return None


def expiry_view(
    kind: Optional[ExpiryKind], ttl_days: Optional[int], expires_at: Optional[datetime], timezone: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """`stored_expiry` as the model sends it, plus `due` (a local ISO date) for a horizon. No decision is no view: the
    model is not shown a decision it could not resend."""
    expiry = stored_expiry(kind, ttl_days, expires_at, timezone)
    if expiry is None:
        return None, None
    if expiry.kind is ExpiryKind.NONE:
        return {"kind": "none"}, None
    if expiry.on is not None:  # kind date (Expiry's own invariant)
        return {"kind": "date", "date": expiry.on.isoformat()}, None
    due = local_date(expires_at, timezone).isoformat() if expires_at is not None else None
    return {"kind": "horizon", "days": expiry.days}, due


def expiry_refusal(expiry: Expiry) -> Optional[str]:
    """Why a decided `expiry` may not be written, or None: the bounds `parse_expiry` holds a model's expiry to (a horizon
    of 1 to MAX_HORIZON_DAYS whole days, a date's year between MIN_YEAR and MAX_YEAR), in the parser's words without its
    advice to the model. Not checked by `Expiry` itself: a stored legacy value is read into one as it is."""
    if expiry.on is not None and not _year_in_range(expiry.on):
        return f"expiry: {_year_refusal(expiry.on)}"
    if expiry.kind is ExpiryKind.HORIZON and not _days_in_range(expiry.days):
        return f"expiry: {_days_refusal(expiry.days)}"
    return None


def _malformed(why: str) -> FactExpiryError:
    return FactExpiryError(f"Invalid expiry: {why}. Use one of {EXPIRY_SHAPES}. {NOTHING_WRITTEN}")


def _year_in_range(day: date) -> bool:
    return MIN_YEAR <= day.year <= MAX_YEAR


def _year_refusal(day: date) -> str:
    return f"the year of `date` must be between {MIN_YEAR} and {MAX_YEAR}, got {day.isoformat()}"


def _days_in_range(days: object) -> bool:
    return isinstance(days, int) and not isinstance(days, bool) and 1 <= days <= MAX_HORIZON_DAYS


def _days_refusal(days: object) -> str:
    return f"kind horizon needs `days` as a whole number from 1 to {MAX_HORIZON_DAYS}, got {days!r}"


def _parse_day(raw: Any) -> date:
    day: Optional[date] = None
    if isinstance(raw, datetime):
        day = raw.date()
    elif isinstance(raw, date):
        day = raw
    elif isinstance(raw, str):
        text = raw.strip()
        try:
            day = date.fromisoformat(text) if len(text) == 10 else datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            day = None  # malformed text: refused below with the shapes
    if day is None:
        raise _malformed(f"kind date needs `date` as YYYY-MM-DD, got {raw!r}")
    if not _year_in_range(day):
        raise _malformed(_year_refusal(day))
    return day


def _parse_days(raw: Any) -> int:
    if isinstance(raw, float) and raw.is_integer():  # a tool-call transport may hand 14 over as 14.0
        raw = int(raw)
    elif isinstance(raw, str) and raw.strip().isascii() and raw.strip().isdigit():  # "90": accept liberally
        raw = int(raw.strip())
    if _days_in_range(raw):
        days: int = raw
        return days
    raise _malformed(_days_refusal(raw))

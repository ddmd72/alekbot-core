"""The `expiry` argument of the fact tools: parsing it, and showing a stored decision to the model in the shape it sends."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Final, Optional, Tuple

from .entities import ExpiryKind
from .exceptions import FactExpiryError
from .local_time import local_date, local_moment

NOTHING_WRITTEN: Final[str] = "Nothing was written. Resend this call with a valid expiry before retiring any fact it replaces."
EXPIRY_SHAPES: Final[str] = "{kind: date, date: YYYY-MM-DD}, {kind: horizon, days: N} or {kind: none}"


@dataclass(frozen=True)
class Expiry:
    kind: ExpiryKind
    on: Optional[date] = None      # kind date: the last day the fact holds
    days: Optional[int] = None     # kind horizon: days counted from when the owner said it


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


def expiry_view(
    kind: Optional[ExpiryKind], ttl_days: Optional[int], expires_at: Optional[datetime], timezone: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """A stored decision as the model sends it, plus `due` (a local ISO date) for a horizon.

    Sending the view back unchanged keeps the same `expires_at`. No kind, or a kind without its data, is no view: the
    model is not shown a decision it could not resend.
    """
    if kind is ExpiryKind.NONE:
        return {"kind": "none"}, None
    if kind is ExpiryKind.DATE and expires_at is not None:
        # One microsecond before the next midnight is the last moment of the day, DST-safe (a day can be 23 h).
        last_day = local_date(local_moment(expires_at, "UTC") - timedelta(microseconds=1), timezone)
        return {"kind": "date", "date": last_day.isoformat()}, None
    if kind is ExpiryKind.HORIZON and ttl_days is not None:
        due = local_date(expires_at, timezone).isoformat() if expires_at is not None else None
        return {"kind": "horizon", "days": ttl_days}, due
    return None, None


def _malformed(why: str) -> FactExpiryError:
    return FactExpiryError(f"Invalid expiry: {why}. Use one of {EXPIRY_SHAPES}. {NOTHING_WRITTEN}")


def _parse_day(raw: Any) -> date:
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        try:
            return date.fromisoformat(text) if len(text) == 10 else datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            pass
    raise _malformed(f"kind date needs `date` as YYYY-MM-DD, got {raw!r}")


def _parse_days(raw: Any) -> int:
    if isinstance(raw, float) and raw.is_integer():  # a tool-call transport may hand 14 over as 14.0
        raw = int(raw)
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1:
        return raw
    raise _malformed(f"kind horizon needs `days` as a whole number of at least 1, got {raw!r}")

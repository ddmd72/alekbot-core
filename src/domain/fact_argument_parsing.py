"""Parsing of the enum and date arguments the fact tools receive from the model."""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, TypeVar

from .local_time import parse_local_stamp

E = TypeVar("E", bound=Enum)


def parse_enum(enum_cls: type[E], name: str, raw: Any) -> E:
    """`raw` as a member of `enum_cls`, case-insensitively. Raises ValueError naming the allowed values."""
    allowed = ", ".join(member.value for member in enum_cls)
    if not isinstance(raw, str):
        raise ValueError(f"{name} must be a string, one of: {allowed}")
    try:
        return enum_cls(raw.strip().lower())
    except ValueError:
        raise ValueError(f"Invalid {name} {raw!r}; use one of: {allowed}") from None


def parse_reported_date(raw: Any, timezone_name: Optional[str] = None) -> Optional[datetime]:
    """An ISO-8601 string (a trailing Z allowed) or a datetime, always aware (a naive value is UTC); None and "" mean no date.

    With the owner's zone, the stamp the consolidator shows its conversation (`2026-10-22 Thu 12:00 CEST`) is read too.
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return _aware(raw)
    if not isinstance(raw, str):
        raise ValueError("reported_date must be an ISO-8601 date string")
    try:
        return _aware(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        stamped = parse_local_stamp(raw, timezone_name) if timezone_name else None
        if stamped is not None:
            return stamped
        raise ValueError(f"Invalid reported_date {raw!r}; use an ISO-8601 timestamp with its offset, such as 2026-03-04T10:00:00+01:00") from None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

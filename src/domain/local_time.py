"""One timestamp format, in the owner's calendar: Smart's message stamps and the consolidator's both come from here."""

import re
from datetime import date, datetime
from typing import Optional, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_UTC = ZoneInfo("UTC")
_STAMP = re.compile(r"(\d{4}-\d{2}-\d{2}) ([A-Za-z]{3}) (\d{2}:\d{2}) (\S+)")


def zone_or_utc(timezone: str) -> ZoneInfo:
    """The IANA zone `timezone` names; an empty, unknown or malformed name is UTC."""
    try:
        return ZoneInfo(timezone or "UTC")
    except (ZoneInfoNotFoundError, KeyError, ValueError, OSError):
        return _UTC


def local_moment(moment: Union[float, datetime], timezone: str) -> datetime:
    """`moment` (epoch seconds, or a datetime: naive means UTC) as an aware datetime in the zone."""
    zone = zone_or_utc(timezone)
    if isinstance(moment, datetime):
        return moment.astimezone(zone) if moment.tzinfo is not None else moment.replace(tzinfo=_UTC).astimezone(zone)
    return datetime.fromtimestamp(moment, tz=zone)


def local_stamp(moment: Union[float, datetime], timezone: str) -> str:
    """`2026-10-22 Thu 12:00 CEST`: year, weekday and the zone's abbreviation of the moment's own date.

    A November moment says CET even when it is read in July. An unknown zone is UTC.
    """
    local = local_moment(moment, timezone)
    return local.strftime(f"%Y-%m-%d %a %H:%M {local.strftime('%Z') or timezone or 'UTC'}")


def local_day(moment: Union[float, datetime], timezone: str) -> str:
    """`2026-10-22 Thu`."""
    return local_moment(moment, timezone).strftime("%Y-%m-%d %a")


def local_date(moment: Union[float, datetime], timezone: str) -> date:
    """The calendar date of `moment` in the zone."""
    return local_moment(moment, timezone).date()


def parse_local_stamp(text: str, timezone: str) -> Optional[datetime]:
    """The moment a `local_stamp` in this zone names, or None.

    Only the exact stamp the zone would render is read: the weekday and the abbreviation must match that local time
    (the abbreviation also picks between the two readings of a repeated autumn hour).
    """
    found = _STAMP.fullmatch(text.strip())
    if found is None:
        return None
    day, weekday, clock, abbreviation = found.groups()
    try:
        naive = datetime.fromisoformat(f"{day}T{clock}")
    except ValueError:
        return None
    zone = zone_or_utc(timezone)
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        rendered = candidate.strftime("%Z") or timezone or "UTC"
        exists = candidate.astimezone(_UTC).astimezone(zone).replace(tzinfo=None) == naive  # not a skipped spring hour
        if exists and rendered == abbreviation and candidate.strftime("%a") == weekday:
            return candidate
    return None

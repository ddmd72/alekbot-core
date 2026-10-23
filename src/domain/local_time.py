"""One timestamp format, in the owner's calendar: Smart's message stamps and the consolidator's both come from here."""

from datetime import date, datetime
from typing import Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_UTC = ZoneInfo("UTC")


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

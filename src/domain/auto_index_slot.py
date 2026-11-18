"""When a user's daily Gmail auto-index is due: the local slot that contains a tick (P16-D1).

The scheduler ticks hourly and the user picks a local hour. A slot is the one hour starting at that wall time on a local
date, as an instant: on a spring-forward day whose hour does not exist, the slot starts where the wall time would have
been (the clock then reads the next hour); on a fall-back day whose hour happens twice, it is the first pass. A tick
inside the slot is the one that may start the day's job; which date it belongs to is the key the stored claim uses, so
a second tick of the same slot, or the repeated hour, finds the day already claimed.
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SLOT = timedelta(hours=1)


def auto_index_slot_date(now_utc: datetime, zone_name: str, hour: int) -> date | None:
    """The local date whose `hour`:00 slot contains `now_utc`, or None when no slot does.

    Raises ZoneInfoNotFoundError or ValueError for a zone name that is not an IANA key: the caller decides what an
    unusable stored zone means for its user.
    """
    tz = ZoneInfo(zone_name)
    now = now_utc.astimezone(timezone.utc)
    today = now.astimezone(tz).date()
    for local_date in (today, today - timedelta(days=1)):  # an 23:00 slot runs past local midnight
        start = datetime(local_date.year, local_date.month, local_date.day, hour, tzinfo=tz).astimezone(timezone.utc)
        if start <= now < start + SLOT:
            return local_date
    return None

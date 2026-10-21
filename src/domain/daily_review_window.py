"""Daily email review window: which local date's review slot an hourly tick falls in."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

REVIEW_WINDOW_HOURS = 3


@dataclass(frozen=True)
class EligibleReviewUser:
    user_id: str
    account_id: str
    slot_date: date


def review_slot_date(
    now_utc: datetime, tz: ZoneInfo, configured_hour: int
) -> Optional[date]:
    """Local date of the review slot open at now_utc, or None outside the window.

    The window is the first REVIEW_WINDOW_HOURS hours from configured_hour and wraps
    midnight; the slot date is the local date on which the window opened.
    """
    if not 0 <= configured_hour <= 23:
        return None
    local = now_utc.astimezone(tz)
    offset = (local.hour - configured_hour) % 24
    if offset >= REVIEW_WINDOW_HOURS:
        return None
    return (local - timedelta(hours=offset)).date()

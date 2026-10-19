"""
Email date convention: naive UTC.

The Gmail adapter strips the timezone from every parsed `Date` header and the Firestore repos
strip it on read, so email dates and indexing cursors are naive UTC. A tz-aware value mixed into
a comparison raises "can't compare offset-naive and offset-aware datetimes".
"""

from datetime import datetime, timezone
from typing import Optional


def to_naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Return `dt` as naive UTC; naive input is assumed to be UTC already."""
    if dt is None or dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)

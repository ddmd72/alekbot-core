"""When a stored lease still blocks: the one liveness rule of the janitor's run guard and the memory-write lease."""

from datetime import datetime
from typing import Optional


def lease_held(held_until: Optional[datetime], now: datetime) -> bool:
    """Whether a stored lease still blocks at `now`: it does until its expiry instant; a lease expiring exactly `now`,
    an absent one or one without an expiry is free."""
    return held_until is not None and held_until > now

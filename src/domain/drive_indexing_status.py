"""
DriveIndexingStatus — what the Cabinet shows about a user's drive index (USER_DRIVE_INDEXING_RFC §6):
counters, freshness, the error category, the readable chunks (Slice 2+4), and Search v2's model dollars: what this
month's cap of each class has paid (the initial build's dollars left out, XC-4), and the initial build's progress (§25.4.5; the §24.7 page and image counters are retired).
Built by DriveSearchService.indexing_status, so the Cabinet and search derive `building` and `index_full` one way.
Pure value object.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Optional

from .drive_index_params import ACCESS_LOSS_TTL


@dataclass(frozen=True)
class DriveIndexingStatus:
    enabled: bool
    counters: Mapping[str, int]
    last_success_at: Optional[datetime] = None
    last_error: Optional[str] = None          # an ErrorCategory value, never a message
    index_as_of: Optional[datetime] = None
    index_full: bool = False
    building: bool = False
    access_lost_at: Optional[datetime] = None
    purging: bool = False
    readable_chunks: int = 0                  # chunks of the generations search may return (§3.3, §7.4)
    usd_month: Mapping[str, float] = field(default_factory=dict)   # UsageClass value -> dollars this month (§25.4.5)
    usd_caps: Mapping[str, float] = field(default_factory=dict)    # UsageClass value -> the month's cap
    initial_build_spent_usd: float = 0.0      # the initial build (D27): spent of its allowance, open or closed
    initial_build_allowance_usd: float = 0.0
    initial_build_closed: bool = False
    awaiting_rebuild: bool = False            # a v1 index not rebuilt under v2 yet (D30): no tick work until then

    def index_kept(self, now: datetime) -> bool:
        """A stored index exists and is inside the access-loss grace period (§7.7): the only time the Cabinet
        may say it is kept, or that search still answers from it. A purge in progress keeps nothing (as the
        search gate, §10.1 step 0)."""
        if self.purging or not any(int(n) > 0 for n in self.counters.values()):
            return False
        return self.access_lost_at is None or now < self.access_lost_at + ACCESS_LOSS_TTL

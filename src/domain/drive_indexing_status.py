"""
DriveIndexingStatus — what the Cabinet shows about a user's drive index (USER_DRIVE_INDEXING_RFC §6):
counters, freshness, the error category. Built by DriveSearchService.indexing_status, so the Cabinet and
search derive `building` and `index_full` one way. Pure value object.
"""
from __future__ import annotations

from dataclasses import dataclass
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

    def index_kept(self, now: datetime) -> bool:
        """A stored index exists and is inside the access-loss grace period (§7.7): the only time the Cabinet
        may say it is kept, or that search still answers from it. A purge in progress keeps nothing (as the
        search gate, §10.1 step 0)."""
        if self.purging or not any(int(n) > 0 for n in self.counters.values()):
            return False
        return self.access_lost_at is None or now < self.access_lost_at + ACCESS_LOSS_TTL

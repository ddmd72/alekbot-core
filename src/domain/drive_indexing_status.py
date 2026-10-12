"""
DriveIndexingStatus — what the Cabinet shows about a user's drive index (USER_DRIVE_INDEXING_RFC §6):
counters, freshness, the error category. Built by DriveSearchService.indexing_status, so the Cabinet and
search derive `building` and `index_full` one way. Pure value object.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Optional


@dataclass(frozen=True)
class DriveIndexingStatus:
    enabled: bool
    counters: Mapping[str, int]
    last_success_at: Optional[datetime] = None
    last_error: Optional[str] = None          # an ErrorCategory value, never a message
    index_as_of: Optional[datetime] = None
    index_full: bool = False
    building: bool = False

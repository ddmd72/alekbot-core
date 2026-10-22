"""The drive specialist's preflight: the `drive` and `search` lines of its state block (DRIVE_SPECIALIST_RFC §5.3)."""
from __future__ import annotations

from dataclasses import dataclass

from .drive_line import DriveLine
from .drive_search_line import SearchLine


@dataclass(frozen=True)
class DrivePreflight:
    drive: DriveLine
    search: SearchLine

"""The drive specialist's preflight: the `drive` and `search` lines of its state block (DRIVE_SPECIALIST_RFC §5.3) — the
primary's — and one `drives` line per live connection (MULTI_DRIVE_RFC §11.2; empty: no `drives` block)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from .drive_line import DriveLine
from .drive_mount_line import MountLine
from .drive_search_line import SearchLine


@dataclass(frozen=True)
class DrivePreflight:
    drive: DriveLine
    search: SearchLine
    mounts: Tuple[MountLine, ...] = ()

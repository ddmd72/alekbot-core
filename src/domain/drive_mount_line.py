"""One line of the drive specialist's `drives` block and of the `/` listing (MULTI_DRIVE_RFC §11.2):
`drive_mounts.mount_line` builds it, `drive_mounts.render_mount_line` prints it."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .drive_line import DriveLine
from .drive_search_line import SearchLine


@dataclass(frozen=True)
class MountLine:
    name: str
    primary: bool
    drive: DriveLine                # connected | reconnect needed
    search: Optional[SearchLine]    # None in the `/` listing (no sync-state read there)
    writable: bool                  # 2a: == primary
    searched_by_default: bool       # 2a: == primary; 2b: the connection's flag

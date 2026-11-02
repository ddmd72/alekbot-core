"""One line of the drive specialist's `drives` block and of the `/` listing (MULTI_DRIVE_RFC §11.2, §11.8, §11.10):
`drive_mounts.mount_line` builds it, `drive_mounts.render_mount_line` prints it."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from .drive_line import DriveLine
from .drive_search_line import SearchLine


@dataclass(frozen=True)
class MountLine:
    name: str
    primary: bool
    drive: DriveLine                # connected | reconnect needed
    search: Optional[SearchLine]    # None in the `/` listing (no sync-state read there)
    writable: bool                  # the connection has a write folder
    searched_by_default: bool       # the connection's flag (D4)
    write_folder: str = ""          # the canonical path of a write folder below the root; "" = the root or unknown
    write_folder_unknown: bool = False  # a write folder below the root whose path could not be read
    note: str = ""                  # the owner's note on the drive, already bounded (`drive_note_line`)
    scope_folders: Tuple[str, ...] = ()  # canonical paths of the chosen folders (G11), at most the caller's limit
    scope_more: int = 0             # how many chosen folders the line does not name

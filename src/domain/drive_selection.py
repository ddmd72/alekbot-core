"""What a search's `drives` value resolved to (MULTI_DRIVE_RFC §11.2; slice 2a: exactly one mount):
`drive_mounts.resolve_drives`."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from .drive_mount import Mount


@dataclass(frozen=True)
class DriveSelection:
    mounts: Tuple[Mount, ...]
    not_searched: Tuple[str, ...] = ()   # current names of the live mounts not in `mounts`, table order
    notice: str = ""                     # RENAME_NOTICE when a former name was used

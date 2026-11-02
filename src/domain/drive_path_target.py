"""A path the model wrote, resolved against the mount tree (MULTI_DRIVE_RFC §11.1): `drive_mounts.resolve_path`."""
from __future__ import annotations

from dataclasses import dataclass

from .drive_mount import Mount


@dataclass(frozen=True)
class PathTarget:
    mount: Mount
    path: str          # connection-relative, segments joined by "/", "" = the mount's root
    named: bool        # the first segment named the mount (current or held former name)
    notice: str = ""   # RENAME_NOTICE when a held former name matched, else ""

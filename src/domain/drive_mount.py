"""One live drive connection as the mount tree sees it (MULTI_DRIVE_RFC §11.1, §11.8)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from .drive_connection import DriveConnection


@dataclass(frozen=True)
class Mount:
    connection: DriveConnection        # ACTIVE or NEEDS_RECONNECT only
    primary: bool
    former_names: Tuple[str, ...] = ()  # held former names (§11.8), newest first; never the current name

    @property
    def name(self) -> str:
        return self.connection.mount_name

    @property
    def connection_id(self) -> str:
        return self.connection.connection_id

    @property
    def ref_tag(self) -> str:
        return self.connection.ref_tag

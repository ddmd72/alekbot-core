"""The user's mount tree at one instant (MULTI_DRIVE_RFC §11.1): live connections, oldest first, and the primary.
Built by `drive_mounts.mount_table`."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from .drive_mount import Mount
from .drive_mount_name_change import mount_name_key


@dataclass(frozen=True)
class MountTable:
    mounts: Tuple[Mount, ...] = ()

    def primary(self) -> Optional[Mount]:
        """The mount the primary pointer names; None when it names no live connection."""
        return next((m for m in self.mounts if m.primary), None)

    def names(self) -> Tuple[str, ...]:
        """The current names, in table order."""
        return tuple(m.name for m in self.mounts)

    def by_name(self, name: str) -> Optional[Tuple[Mount, bool]]:
        """(mount, via_former_name) for `name` compared by `mount_name_key`; current names win over held former names
        (validation keeps them apart). None when no mount answers to it."""
        key = mount_name_key(name)
        if not key:
            return None
        for mount in self.mounts:
            if mount_name_key(mount.name) == key:
                return mount, False
        for mount in self.mounts:
            if any(mount_name_key(former) == key for former in mount.former_names):
                return mount, True
        return None

    def by_connection(self, connection_id: str) -> Optional[Mount]:
        return next((m for m in self.mounts if m.connection_id == connection_id), None)

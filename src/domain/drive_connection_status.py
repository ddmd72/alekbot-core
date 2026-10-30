"""What the drive gate, search and the Cabinet's drive status read about a connection (MULTI_DRIVE_RFC §3).

The successor of the per-user connection status of the single-drive code, without the drive id (which lives on the
`DriveConnection` as `drive_identity`). Its one stage-1 consumer, `/api/drive/status`, retires with the Cabinet's
connection list in stage 2.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .drive_connection import DriveConnection
from .drive_connection_state import ConnectionState


@dataclass(frozen=True)
class DriveConnectionStatus:
    connected: bool
    needs_reconnect: bool = False

    @classmethod
    def of(cls, connection: Optional[DriveConnection]) -> DriveConnectionStatus:
        """None or DISCONNECTED: not connected; NEEDS_RECONNECT: connected, needs a reconnect; ACTIVE: connected."""
        if connection is None or connection.state is ConnectionState.DISCONNECTED:
            return cls(connected=False)
        return cls(connected=True, needs_reconnect=connection.state is ConnectionState.NEEDS_RECONNECT)

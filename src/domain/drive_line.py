"""The `drive` line of the drive specialist's state block (DRIVE_SPECIALIST_RFC §5.3): can the drive be reached. Read
from the primary connection's status (MULTI_DRIVE_RFC §4.4; one line per connection is stage 2)."""
from __future__ import annotations

from enum import Enum

from .drive_connection_status import DriveConnectionStatus


class DriveLine(str, Enum):
    CONNECTED = "connected"
    RECONNECT_NEEDED = "reconnect needed"
    NOT_CONNECTED = "not connected"
    NOT_CONFIGURED = "not configured"

    @classmethod
    def of(cls, connection: DriveConnectionStatus) -> "DriveLine":
        if not connection.connected:
            return cls.NOT_CONNECTED
        return cls.RECONNECT_NEEDED if connection.needs_reconnect else cls.CONNECTED

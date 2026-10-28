"""The `drive` line of the drive specialist's state block (DRIVE_SPECIALIST_RFC §5.3): can the drive be reached."""
from __future__ import annotations

from enum import Enum

from .user_drive import DriveConnection


class DriveLine(str, Enum):
    CONNECTED = "connected"
    RECONNECT_NEEDED = "reconnect needed"
    NOT_CONNECTED = "not connected"
    NOT_CONFIGURED = "not configured"

    @classmethod
    def of(cls, connection: DriveConnection) -> "DriveLine":
        if not connection.connected:
            return cls.NOT_CONNECTED
        return cls.RECONNECT_NEEDED if connection.needs_reconnect else cls.CONNECTED

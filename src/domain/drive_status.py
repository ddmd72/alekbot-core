"""The status line of the drive specialist's answer (DRIVE_SPECIALIST_RFC §7.2): set by code, never by the model."""
from __future__ import annotations

from enum import Enum


class DriveStatus(str, Enum):
    STILL_RUNNING = "still_running"
    UNFINISHED = "unfinished"
    PARTIAL = "partial"
    REFUSED = "refused"
    FAILED = "failed"
    NOT_FOUND = "not_found"
    NOT_CONNECTED = "not_connected"
    NOT_POSSIBLE = "not_possible"
    AMBIGUOUS = "ambiguous"
    OK = "ok"
    REBUILDING = "rebuilding"
    INDEXING_OFF = "indexing_off"

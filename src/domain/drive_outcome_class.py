"""The class of one drive operation's outcome (DRIVE_SPECIALIST_RFC §7.2): what the answer's status is built from."""
from __future__ import annotations

from enum import Enum


class OutcomeClass(str, Enum):
    """§7.2. `REQUEST_ERROR` is a request-shape error the specialist corrects in its loop (both or neither source, an
    empty field, a bad date or kind); one it never corrected counts as `REFUSED`. `NOT_FINISHED` is a read cut by the
    soft deadline or a call not started after it."""
    OK = "ok"
    REQUEST_ERROR = "request_error"
    NOT_CONNECTED = "not_connected"
    INDEXING_OFF = "indexing_off"
    REBUILDING = "rebuilding"
    NOT_FOUND = "not_found"
    REFUSED = "refused"
    FAILED = "failed"
    NOT_POSSIBLE = "not_possible"
    STILL_RUNNING = "still_running"
    NOT_FINISHED = "not_finished"

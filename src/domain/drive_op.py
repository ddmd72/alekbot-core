"""One drive operation (DRIVE_SPECIALIST_RFC §5.5): the key a `DriveOutcome` and the late-failure notices are filed
under."""
from __future__ import annotations

from enum import Enum


class DriveOp(str, Enum):
    """One drive operation (§5.5). Late-failure notices are keyed on it (`DELETE`, `REPLACE`), not on an intent."""
    SEARCH = "search"
    LIST = "list"
    OPEN = "open"
    COPY = "copy"
    SAVE_TEXT = "save_text"
    CREATE_FOLDER = "create_folder"
    MOVE = "move"
    APPEND = "append"
    REPLACE = "replace"
    DELETE = "delete"


MUTATING_OPS = frozenset({DriveOp.COPY, DriveOp.SAVE_TEXT, DriveOp.CREATE_FOLDER, DriveOp.MOVE, DriveOp.APPEND,
                          DriveOp.REPLACE, DriveOp.DELETE})

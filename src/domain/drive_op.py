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
    # The owner's notes on drive items (specialist RFC §9.2): a read, a change of the note, an erase request.
    NOTE_READ = "note_read"
    NOTE_WRITE = "note_write"
    NOTE_ERASE = "note_erase"
    # Both passages tools, a read (USER_DRIVE_INDEXING_RFC §28.3 A3): not in MUTATING_OPS.
    PASSAGES = "passages"


MUTATING_OPS = frozenset({DriveOp.COPY, DriveOp.SAVE_TEXT, DriveOp.CREATE_FOLDER, DriveOp.MOVE, DriveOp.APPEND,
                          DriveOp.REPLACE, DriveOp.DELETE, DriveOp.NOTE_WRITE, DriveOp.NOTE_ERASE})

"""STUB (part A) — the typed outcome of one drive operation (DRIVE_SPECIALIST_RFC §5.5, §7.2).

Part A owns this module; part B uses only the names below. Several value types of one contract in one module (the
`drive_enrichment.py` waiver).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from .user_drive import DriveItem


class DriveOp(str, Enum):
    """One drive operation (§5.5). Late-failure notices are keyed on it (DELETE, REPLACE), not on intent names."""
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


class DriveOutcome(str, Enum):
    """An operation's outcome class (§7.2 table 2)."""
    OK = "ok"
    STILL_RUNNING = "still_running"
    NOT_CONNECTED = "not_connected"
    INDEXING_OFF = "indexing_off"
    REBUILDING = "rebuilding"
    NOT_FOUND = "not_found"
    REFUSED = "refused"
    FAILED = "failed"


@dataclass(frozen=True)
class DriveOpOutcome:
    """Today's text (success or error), the drive items it touched, any vision file, and the outcome class."""
    op: DriveOp
    outcome: DriveOutcome
    text: str
    items: Tuple[DriveItem, ...] = ()
    file_data: Optional[Dict[str, Any]] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome is DriveOutcome.OK

    def context_entries(self) -> List[Dict[str, str]]:
        from .user_drive import drive_context_entry
        return [drive_context_entry(i) for i in self.items]

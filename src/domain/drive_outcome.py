"""The typed outcome of one drive operation (DRIVE_SPECIALIST_RFC §5.5, §7.2).

`DriveOperations` (src/infrastructure/drive_operations.py) returns one `DriveOutcome` per call: today's text (success
or error, byte for byte), whether today's code answered it as a success, the outcome class the answer's status is
built from, the drive items touched (the `drive_context` entries), the vision file, and the facts about a read that
decide the reading lines (§7.3). FileManagementAgent maps it back to today's AgentResponse; the drive specialist feeds
it to the pass-through collector.

Waiver (one public class per file): `DriveOp`, `OutcomeClass`, `ReadFacts` and `DriveOutcome` are the value types of one
contract, the drive-domain precedent of `drive_enrichment.py`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from .user_drive import DriveItem, drive_context_entry


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


@dataclass(frozen=True)
class ReadFacts:
    """What a read showed, decided by code from the typed result (never from its text): the triggers of §7.3."""
    cards: int = 0                       # search cards shown
    passage: bool = False                # a card carries a passage
    accuracy: bool = False               # a card carries `card accuracy: N%`
    scan: bool = False                   # a card's capture is a scan or a photo of a document
    inaccessible: bool = False           # a card, or the index's access note, says the bot cannot open the file
    image: bool = False                  # an image card
    coverage_incomplete: bool = False    # the coverage line names something not searched
    building: bool = False               # the index is still being built or rebuilt


@dataclass(frozen=True)
class DriveOutcome:
    """One operation's result. `success` is whether today's code answered with AgentResponse.success (a search that
    found nothing, a still-running mutation and the HEIC refusal are successes today); `cls` is the §7.2 class."""
    op: DriveOp
    cls: OutcomeClass
    text: str
    success: bool
    items: Tuple[DriveItem, ...] = ()
    file_data: Optional[Dict[str, Any]] = None
    facts: ReadFacts = field(default_factory=ReadFacts)

    @property
    def is_change(self) -> bool:
        return self.op in MUTATING_OPS

    def drive_context(self) -> List[Dict[str, str]]:
        """Today's per-operation `drive_context` entries (§7.4)."""
        return [drive_context_entry(item) for item in self.items]

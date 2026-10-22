"""The typed outcome of one drive operation (DRIVE_SPECIALIST_RFC §5.5, §7.2).

`DriveOperations` (src/infrastructure/drive_operations.py) returns one `DriveOutcome` per call: today's text (success
or error, byte for byte), whether today's code answered it as a success, the outcome class the answer's status is
built from, the drive items touched (the `drive_context` entries), the vision file, and the facts about a read that
decide the reading lines (§7.3). FileManagementAgent maps it back to today's AgentResponse; the drive specialist feeds
it to the pass-through collector. Its parts live beside it: `DriveOp` (drive_op.py), `OutcomeClass`
(drive_outcome_class.py), `ReadFacts` (drive_read_facts.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .drive_op import MUTATING_OPS, DriveOp
from .drive_outcome_class import OutcomeClass
from .drive_read_facts import ReadFacts
from .user_drive import DriveItem, drive_context_entry


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

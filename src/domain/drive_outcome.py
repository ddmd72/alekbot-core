"""The typed outcome of one drive operation (DRIVE_SPECIALIST_RFC §5.5, §7.2).

`DriveOperations` (src/infrastructure/drive_operations.py) returns one `DriveOutcome` per call: today's text (success
or error, byte for byte), whether today's code answered it as a success, the outcome class the answer's status is
built from, the drive items touched (the `drive_context` entries), the vision file, and the facts about a read that
decide the reading lines (§7.3). FileManagementAgent maps it back to today's AgentResponse; the drive specialist feeds
it to the pass-through collector. A note tool's outcome (specialist RFC §9.2) also carries the note's history line
(`note_context`, `[Drive note: <path> ref=… v<n>]`) and the receipt or erase request for the owner (`delivery`), so both
reach the answer from the collector on every exit, a cut run included (design Δ7). Its parts live beside it: `DriveOp` (drive_op.py), `OutcomeClass`
(drive_outcome_class.py), `ReadFacts` (drive_read_facts.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .agent import DeliveryItem
from .drive_excerpt_context_plan import ContextPlan
from .drive_excerpt_result import ExcerptResult
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
    # The text is a file's content (opened text, a transcript, a recording link): the drive specialist's model sees a
    # stand-in, the orchestrator the text (DRIVE_SPECIALIST_RFC §6).
    body: bool = False
    # A note change: its text-free history line and its receipt (or erase request) for the owner's chat.
    note_context: Tuple[str, ...] = ()
    delivery: Tuple[DeliveryItem, ...] = ()
    # A passages call (USER_DRIVE_INDEXING_RFC §28.3 A10, A11): its typed result and the context plan its text
    # printed, for the run's ledger; `request_text` is the search query or the passages question (the Searched line).
    excerpts: Optional[ExcerptResult] = None
    excerpt_plan: Optional[ContextPlan] = None
    request_text: str = ""

    @property
    def is_change(self) -> bool:
        return self.op in MUTATING_OPS

    def drive_context(self) -> List[Dict[str, str]]:
        """Today's per-operation `drive_context` entries (§7.4)."""
        return [drive_context_entry(item) for item in self.items]

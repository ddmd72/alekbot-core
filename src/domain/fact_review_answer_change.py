"""How one owner answer changes its fact: what the write path is given, decided in the domain."""
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class AnswerChange:
    """One of two writes. `updates` goes through `update_fact` (the S2 preconditions and the review path) with `reason`
    for the trail, `evidence_at` (the moment of the answer: a revive's evidence date, and the date of the history fact)
    and `history` (the text of the fact to create beside an archive, None for none). `fields` is an in-place write of
    the question's fallback (`fallback_fields`), whose trail entry is already inside it. The question is not touched
    here: its close is the caller's, through the question's own methods."""

    updates: Optional[Dict[str, Any]] = None
    reason: Optional[str] = None
    evidence_at: Optional[datetime] = None
    history: Optional[str] = None
    fields: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        if self.fields is not None:
            if any(part is not None for part in (self.updates, self.reason, self.evidence_at, self.history)):
                raise ValueError("an in-place fallback carries no update, reason, evidence date or history")
        elif self.updates is None or not self.reason or self.evidence_at is None:
            raise ValueError("an update needs its updates, its reason and the moment of the answer")

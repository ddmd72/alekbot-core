"""What the review model names as the reason for a change (plan §3.5): another fact, the owner's reply, or a passed date."""
from dataclasses import dataclass
from datetime import date
from typing import Literal, Optional

BasisKind = Literal["evidence", "reply", "date_passed"]


@dataclass(frozen=True)
class ReviewBasis:
    """`evidence`: `evidence_fact_id`. `reply`: `question_id` and a `quote` of the owner's words. `date_passed`: `on`, the
    date of the plan (a `date` fact uses its own). The fields of the other kinds stay unset."""

    kind: BasisKind
    evidence_fact_id: Optional[str] = None
    question_id: Optional[str] = None
    quote: Optional[str] = None
    on: Optional[date] = None

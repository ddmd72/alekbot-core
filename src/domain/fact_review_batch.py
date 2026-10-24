"""What one review pass is given to decide (plan §3.2 step 8): the batch's facts and the open questions about them."""
from dataclasses import dataclass
from typing import Tuple

from .entities import FactEntity
from .fact_review_question import FactReviewQuestion


@dataclass(frozen=True)
class ReviewBatch:
    """`facts`: the batch facts as read when the pass began; each stands for its lineage (`lineage_id`). `questions`: the
    open questions about them, with any recorded reply; a timed-out candidate's question is among them
    (`FactReviewQuestion.is_timed_out`)."""

    facts: Tuple[FactEntity, ...]
    questions: Tuple[FactReviewQuestion, ...] = ()

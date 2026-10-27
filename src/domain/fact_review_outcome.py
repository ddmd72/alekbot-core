"""What a review pass did to one batch fact (plan §3.3, §3.5), read from the state of the document its write left."""
from dataclasses import dataclass
from typing import Literal, Optional

from .entities import FactEntity, FactState
from .fact_review_basis import BasisKind

ReviewOutcome = Literal["revived", "rewritten", "archived", "invalidated", "superseded"]
# The outcomes that leave the lineage current: the pass may write it again (R3-13), and it counts as current evidence.
MADE_CURRENT: frozenset[ReviewOutcome] = frozenset({"revived", "rewritten"})

_CLOSED: dict[FactState, ReviewOutcome] = {
    FactState.ARCHIVED: "archived", FactState.INVALIDATED: "invalidated", FactState.SUPERSEDED: "superseded",
}


@dataclass(frozen=True)
class ReviewDecision:
    """One batch fact's result: `fact_id` is the batch fact, `written_id` the document the last write left, `basis_kind`
    that write's basis (None for a supersede), `as_history` whether it rewrote the fact as history (P2-1)."""

    fact_id: str
    outcome: ReviewOutcome
    written_id: str
    basis_kind: Optional[BasisKind]
    as_history: bool = False


def review_outcome(batch_fact: FactEntity, written: FactEntity) -> ReviewOutcome:
    """The outcome of the pass for `batch_fact`, from `written` (the document of its lineage the write left): its closing
    state, else current as a new version (`rewritten`) or as the same document (`revived`).

    Raises ValueError for a stale document: a review write never leaves its fact stale."""
    if written.state in _CLOSED:
        return _CLOSED[written.state]
    if written.state is FactState.CURRENT:
        return "rewritten" if written.id != batch_fact.id else "revived"
    raise ValueError(f"a review write leaves no fact {written.state.value}")

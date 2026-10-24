"""Who writes a fact, and what that pass may do: the one table every reader asks (Facts S4 T2a, hexagonal review H2).

A fact write belongs to a pass. The pass decides whose dates count, whether a due-soon horizon is floored, which stored
states the write may reach, whether an unanswered date refusal is a loss and which wording the refusal gets. These were
three label lists (the agent's, the stats', the write rules' `not model_dates`); they are rows here and nowhere else.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Dict, FrozenSet, Final

from .entities import FactState
from .fact_lifecycle import CONSOLIDATION_ACCEPTS

# The three wordings of a date refusal. The texts need dates and live where the refusal is built; this is the choice.
WORDING_STAGE1: Final[str] = "stage1"
WORDING_CLEANUP: Final[str] = "cleanup"
WORDING_REVIEW: Final[str] = "review"

_REVIEW_ACCEPTS: Final[FrozenSet[FactState]] = frozenset({FactState.STALE, FactState.CURRENT})


class PassActor(str, Enum):
    """The pass a fact write comes from. `PassActor("x")` raises ValueError for a label that is no pass."""

    STAGE1 = "stage1"
    EMAIL = "email"
    STAGE2A = "stage2a"
    STAGE2B = "stage2b"
    CLUSTER = "cluster"
    BACKSTOP = "backstop"
    REVIEW = "review"
    OWNER = "owner"

    @property
    def model_dates(self) -> bool:
        """True = the model's `reported_date` is clamped into the batch span and used (the conversation passes)."""
        return _ROWS[self].model_dates

    @property
    def floors_due_horizon(self) -> bool:
        """True = a horizon that resolves within a day is kept and floored to two days from now, not refused."""
        return _ROWS[self].floors_due_horizon

    @property
    def accepts(self) -> FrozenSet[FactState]:
        """The stored states the write's own target may be in."""
        return _ROWS[self].accepts

    @property
    def born_due_refusal_is_loss(self) -> bool:
        """True = a born-due refusal this pass leaves unanswered loses a stored fact (the 🚨 alert counts it)."""
        return _ROWS[self].born_due_refusal_is_loss

    @property
    def refusal_wording(self) -> str:
        """Which text a date refusal gets: WORDING_STAGE1, WORDING_CLEANUP or WORDING_REVIEW."""
        return _ROWS[self].refusal_wording


@dataclass(frozen=True)
class _Row:
    model_dates: bool
    floors_due_horizon: bool
    accepts: FrozenSet[FactState]
    born_due_refusal_is_loss: bool
    refusal_wording: str


_CONVERSATION = _Row(
    model_dates=True, floors_due_horizon=False, accepts=CONSOLIDATION_ACCEPTS, born_due_refusal_is_loss=False,
    refusal_wording=WORDING_STAGE1,
)
# A cleanup pass has a model that can answer a refusal, and the fact it holds is stored memory: dropping it is a loss.
_CLEANUP = _Row(
    model_dates=False, floors_due_horizon=True, accepts=CONSOLIDATION_ACCEPTS, born_due_refusal_is_loss=True,
    refusal_wording=WORDING_CLEANUP,
)
# Code, no model: nothing to answer a refusal, so none is counted.
_BACKSTOP = _Row(
    model_dates=False, floors_due_horizon=True, accepts=CONSOLIDATION_ACCEPTS, born_due_refusal_is_loss=False,
    refusal_wording=WORDING_CLEANUP,
)
# The review brings a hidden fact back with a horizon counted from now (S4-D5), so it has no floor to fall back on.
_REVIEW = _Row(
    model_dates=False, floors_due_horizon=False, accepts=_REVIEW_ACCEPTS, born_due_refusal_is_loss=True,
    refusal_wording=WORDING_REVIEW,
)
_OWNER = _Row(
    model_dates=False, floors_due_horizon=False, accepts=_REVIEW_ACCEPTS, born_due_refusal_is_loss=False,
    refusal_wording=WORDING_REVIEW,
)

_ROWS: Final[Dict[PassActor, _Row]] = {
    PassActor.STAGE1: _CONVERSATION,
    PassActor.EMAIL: _CONVERSATION,
    PassActor.STAGE2A: _CLEANUP,
    PassActor.STAGE2B: _CLEANUP,
    PassActor.CLUSTER: _CLEANUP,
    PassActor.BACKSTOP: _BACKSTOP,
    PassActor.REVIEW: _REVIEW,
    PassActor.OWNER: _OWNER,
}

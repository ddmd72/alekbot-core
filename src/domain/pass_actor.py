"""Who writes a fact, and what that pass may do: the one table every reader asks (Facts S4 T2a, hexagonal review H2).

A fact write belongs to a pass. The pass decides whose dates count, whether a due-soon horizon is floored, which stored
states the write may reach, whether an unanswered date refusal is a loss and which wording the refusal gets. These were
three label lists (the agent's, the stats', the write rules' `not model_dates`); they are rows here and nowhere else.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Dict, Final, FrozenSet, Optional

from .entities import FactState
from .fact_lifecycle import CONSOLIDATION_ACCEPTS, REVIEW_ACCEPTS
from .fact_lifecycle_day import CounterField
from .refusal_wording import RefusalWording


class PassActor(StrEnum):
    """The pass a fact write comes from. `PassActor("x")` raises ValueError for a label that is no pass; `str()` and
    f-strings give the bare label, which the trail and the stored documents carry."""

    STAGE1 = "stage1"
    EMAIL = "email"
    STAGE2A = "stage2a"
    STAGE2B = "stage2b"
    CLUSTER = "cluster"
    BACKSTOP = "backstop"
    REVIEW = "review"
    OWNER = "owner"
    # A question's history fallback written on silence (P2-3): the owner's row, under its own label in the trail.
    TIMEOUT = "timeout"

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
    def one_result_per_fact(self) -> bool:
        """True = the pass gives each fact one result (the review), so any success of a fact in the pass, before or
        after a refusal of it and closing included, answers the refusal; elsewhere only a later write that keeps the fact
        does (retiring it is the loss the 🚨 alert is for)."""
        return _ROWS[self].one_result_per_fact

    @property
    def refusals_counter(self) -> CounterField:
        """The day counter the pass's refused calls are added to."""
        return _ROWS[self].refusals_counter

    @property
    def refusal_wording(self) -> RefusalWording:
        """Which text a date refusal gets."""
        return _ROWS[self].refusal_wording

    @property
    def closing_reason(self) -> Optional[str]:
        """The trail reason of an in-place state change the caller gave none for; None = the caller must give one (the
        review and the owner, whose reasons are code-built and always passed in)."""
        return _ROWS[self].closing_reason


@dataclass(frozen=True)
class _Row:
    model_dates: bool
    floors_due_horizon: bool
    accepts: FrozenSet[FactState]
    born_due_refusal_is_loss: bool
    refusal_wording: RefusalWording
    closing_reason: Optional[str]
    one_result_per_fact: bool = False
    refusals_counter: CounterField = CounterField.EXPIRY_TOOL_ERRORS


_CONVERSATION = _Row(
    model_dates=True, floors_due_horizon=False, accepts=CONSOLIDATION_ACCEPTS, born_due_refusal_is_loss=False,
    refusal_wording=RefusalWording.STAGE1, closing_reason="model",
)
# A cleanup pass has a model that can answer a refusal, and the fact it holds is stored memory: dropping it is a loss.
_CLEANUP = _Row(
    model_dates=False, floors_due_horizon=True, accepts=CONSOLIDATION_ACCEPTS, born_due_refusal_is_loss=True,
    refusal_wording=RefusalWording.CLEANUP, closing_reason="model",
)
# Code, no model: nothing to answer a refusal, so none is counted.
_BACKSTOP = _Row(
    model_dates=False, floors_due_horizon=True, accepts=CONSOLIDATION_ACCEPTS, born_due_refusal_is_loss=False,
    refusal_wording=RefusalWording.CLEANUP, closing_reason="directive cap",
)
# The review brings a hidden fact back with a horizon counted from now (S4-D5), so it has no floor to fall back on.
_REVIEW = _Row(
    model_dates=False, floors_due_horizon=False, accepts=REVIEW_ACCEPTS, born_due_refusal_is_loss=True,
    refusal_wording=RefusalWording.REVIEW, closing_reason=None,
    one_result_per_fact=True, refusals_counter=CounterField.REVIEW_REFUSALS,
)
_OWNER = _Row(
    model_dates=False, floors_due_horizon=False, accepts=REVIEW_ACCEPTS, born_due_refusal_is_loss=False,
    refusal_wording=RefusalWording.REVIEW, closing_reason=None,
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
    PassActor.TIMEOUT: _OWNER,
}

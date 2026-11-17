"""How much one profile step may do (fact profile RFC §4.4 step 3): its model calls, their parallelism and their budget."""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class FactProfileRunLimits:
    """max_per_run: model calls per run, newest facts first (None: no cap); call_budget_s: a call starts only while the
    lease leaves this many seconds; concurrency: calls in flight at once."""

    max_per_run: Optional[int]
    call_budget_s: float
    concurrency: int = 4

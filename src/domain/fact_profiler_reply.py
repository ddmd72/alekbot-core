"""What the fact profiler returns for one fact (fact profile RFC §4.1): a status, the validated answer when there is
one, and what the call cost.

- `ok`: `answer` is the validated `ProfileAnswer`.
- `invalid`: the model answered but the answer stayed unusable after the parse retry, or stopped at the output limit.
  This fact's failure.
- `refused`: the provider or the model declined this fact's content. This fact's failure.
- `unavailable`: the model cannot be used now for anyone (rate limit, outage, timeout, account, open breaker, a missing
  prompt, anything unexpected). An outage: not charged to the fact.
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .fact_profile_answer import ProfileAnswer


class ProfilerStatus(str, Enum):
    OK = "ok"
    INVALID = "invalid"
    REFUSED = "refused"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class FactProfilerReply:
    status: ProfilerStatus
    answer: Optional[ProfileAnswer]
    model: str    # the model that served (or was asked)
    usd: float    # already billed to the account
    calls: int    # model calls made, the parse retry included

    @staticmethod
    def unavailable(model: str = "") -> "FactProfilerReply":
        """An outage before any call was made."""
        return FactProfilerReply(status=ProfilerStatus.UNAVAILABLE, answer=None, model=model, usd=0.0, calls=0)

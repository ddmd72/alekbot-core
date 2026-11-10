"""FactProfilerPort — profile one stored fact (docs/10_rfcs/FACT_PROFILE_RFC.md §4.1)."""
from abc import ABC, abstractmethod
from typing import Any, Mapping, Optional, Sequence

from ..domain.confirmed_people import ConfirmedPerson
from ..domain.fact_profiler_reply import FactProfilerReply


class FactProfilerPort(ABC):

    @abstractmethod
    async def profile(self, account_id: str, text: str, metadata: Optional[Mapping[str, Any]],
                      people: Sequence[ConfirmedPerson] = ()) -> FactProfilerReply:
        """Profile one fact from its text and metadata; the call is billed to `account_id`.

        `people` are the account's confirmed list (user first); they go before the fact in the message and never into
        the cached system prompt.

        Never raises: every failure is a status. `invalid` and `refused` are this fact's failure (the caller counts
        it against the fact's fingerprint); `unavailable` is an outage (the caller stops and does not count it).
        """

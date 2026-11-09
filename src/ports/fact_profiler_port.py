"""FactProfilerPort — profile one stored fact (docs/10_rfcs/FACT_PROFILE_RFC.md §4.1)."""
from abc import ABC, abstractmethod
from typing import Any, Mapping, Optional

from ..domain.fact_profiler_reply import FactProfilerReply


class FactProfilerPort(ABC):

    @abstractmethod
    async def profile(self, account_id: str, text: str, metadata: Optional[Mapping[str, Any]]) -> FactProfilerReply:
        """Profile one fact from its text and metadata; the call is billed to `account_id`.

        Never raises: every failure is a status. `invalid` and `refused` are this fact's failure (the caller counts
        it against the fact's fingerprint); `unavailable` is an outage (the caller stops and does not count it).
        """

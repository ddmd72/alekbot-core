from abc import ABC, abstractmethod
from typing import AbstractSet, List, Sequence

from ..domain.entities import FactEntity
from ..domain.fact_query import FactQuery


class FactSearchPort(ABC):
    """Port for the fact search (fact profile RFC §5): one query, or one to four phrases' queries, in; the facts that
    answer them in scored order out."""

    @abstractmethod
    async def search(
        self, account_id: str, query: FactQuery, *, exclude: AbstractSet[str] = frozenset()
    ) -> List[FactEntity]:
        """Return the current facts of account `account_id` that answer `query` (one phrase), best first.

        Order: the query's candidates by score (RFC §5.3; ties by fact id), near-duplicates removed in that order.
        Count: at most 30 for `scope == "top"`; every kept fact for `"all"`.
        `exclude`: fact ids that are never candidates (the Router's baseline).
        Projection: the snapshot's (no vectors; profile fields present).
        Tenancy: only that account's facts.
        Errors: an embedding or store failure is logged and raised.
        """
        pass

    @abstractmethod
    async def search_phrases(self, account_id: str, queries: Sequence[FactQuery]) -> List[FactEntity]:
        """Return the facts that answer one to four phrases' queries, given in the caller's priority order.

        Order: each query scored on its own candidates; the fused order of RFC §5.3 "Several phrases" (priority weight
            times σ of the score relative to that phrase's 30th kept score; a fact's best phrase counts); then
            near-duplicates removed in that order. One query is `search` with nothing excluded.
        Count: at most 30; every kept fact when any query has `scope == "all"`.
        Errors: no query or more than four raise ValueError before any I/O; otherwise as `search`.
        Projection and tenancy: as `search`.
        """
        pass

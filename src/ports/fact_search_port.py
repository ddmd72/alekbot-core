from abc import ABC, abstractmethod
from typing import List

from ..domain.entities import FactEntity
from ..domain.fact_query import FactQuery


class FactSearchPort(ABC):
    """Port for the fact search (fact profile RFC §5): one query in, the facts that answer it in fused order."""

    @abstractmethod
    async def search(self, account_id: str, query: FactQuery) -> List[FactEntity]:
        """Return the current facts of account `account_id` that answer `query`, best first.

        Order: reciprocal-rank fusion of the query's lists, near-duplicates removed.
        Count: at most 30 for `scope == "top"`; every kept fact for `"all"`.
        Projection: the snapshot's (no vectors; profile fields present).
        Tenancy: only that account's facts.
        Errors: an embedding or store failure is logged and raised.
        """
        pass

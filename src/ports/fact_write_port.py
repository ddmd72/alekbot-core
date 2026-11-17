"""
FactWritePort — abstract interface for building new facts with their embeddings.

Justification for port promotion:
- Used by FactManagementAdapter (adapters layer), which may not import a service.
- Encapsulates multi-vector embedding generation.
- Enables test doubles without real Gemini embedding calls.
"""
from abc import ABC, abstractmethod
from typing import List, Sequence

from ..domain.entities import FactEntity
from ..domain.new_fact_attributes import NewFactAttributes


class FactWritePort(ABC):
    """Abstract interface for building new facts with automatic multi-vector generation."""

    @abstractmethod
    async def build_facts(
        self,
        account_id: str,
        user_id: str,
        facts: Sequence[NewFactAttributes],
    ) -> List[FactEntity]:
        """
        Embed and build new facts from already-parsed attributes; store nothing and check no duplicates.

        Each entity has a new id and a new lineage, is_current True, version 1 and its three vectors
        (domain.fact_embedding recipe, computed before the system tags are added). Tags gain
        "consolidated" (and "anchor" for a principle); they are appended to the attributes' own list,
        which is not copied. reported_date None means now.
        Returns: one entity per input, in input order; an empty input returns [] with no calls.
        Errors: raises whatever the embedding service raises, and RuntimeError when it returns no vector
            for a fact; no entity is returned with a None vector.
        """

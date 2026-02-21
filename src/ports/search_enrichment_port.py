"""
SearchEnrichmentPort — abstract interface for semantic context enrichment.

Justification for port promotion:
- Used in FactManagementAdapter (adapters layer) and multiple agents.
- Encapsulates an algorithm (RRF multi-vector search) that may change.
- Enables test doubles without wiring real Firestore + embeddings.
"""
from abc import ABC, abstractmethod
from typing import List, Optional, Union

from ..domain.entities import FactEntity
from ..domain.search import EnrichedContext, SearchLimits


class SearchEnrichmentPort(ABC):
    """Abstract interface for multi-vector semantic search with RRF ranking."""

    @abstractmethod
    async def enrich_context(
        self,
        keywords: List[str],
        search_phrase_1: str,
        search_phrase_2: str,
        relevant_domains: Optional[List[str]] = None,
        biographical_facts: Optional[List[Union[FactEntity, dict]]] = None,
        limits: Optional[SearchLimits] = None,
        dedup_threshold: float = 0.98,
        skip_semantic_dedup: bool = False,
    ) -> EnrichedContext:
        """
        Build enriched context using multi-channel search strategy + RRF ranking.

        Returns:
            EnrichedContext with deduplicated, ranked facts.
        """

from abc import ABC, abstractmethod
from typing import List, Dict, Any


class FactManagementPort(ABC):
    """
    Port for deliberate fact management operations.

    Provides the tool surface for ConsolidationAgent v3:
    search → create → update → merge → discard.
    """

    @abstractmethod
    async def search_existing_facts(
        self,
        keywords: List[str],
        primary_query: str,
        alternative_query: str = "",
        limit: int = 20
    ) -> List[Dict[str, Any]]:
        """
        Search existing facts using multi-vector RRF strategy.
        
        Session 2026-02-16: Updated signature for consolidation search
        - 3-key format: keywords + primary_query + alternative_query
        - Multi-vector search (text + metadata + tags)
        - RRF ranking for quality
        - Configurable dedup threshold (implementation detail)
        
        Args:
            keywords: Domain keywords for tag-based search
            primary_query: Main semantic search phrase
            alternative_query: Alternative phrasing (optional)
            limit: Max results to return (default: 20)
            
        Returns:
            List of fact dicts with fact_id, content, similarity, source
        """
        pass

    @abstractmethod
    async def create_fact(self, content: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create a new fact.

        Args:
            content: Fact text
            metadata: Fact metadata (must include account_id, user_id, domain, etc.)

        Returns:
            Result dict with fact_id, status, message
        """
        pass

    @abstractmethod
    async def update_fact(self, fact_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        """
        Update an existing fact.

        Args:
            fact_id: UUID of fact to update
            updates: Fields to update (content, tags, state, etc.)

        Returns:
            Result dict with status, version, message
        """
        pass

    @abstractmethod
    async def merge_facts(
        self,
        fact_ids: List[str],
        merged_content: str,
        metadata: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Merge multiple facts into one enriched fact."""
        pass

    @abstractmethod
    async def discard_candidate(self, reason: str) -> Dict[str, Any]:
        """Explicitly discard a candidate fact."""
        pass
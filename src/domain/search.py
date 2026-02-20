"""
Search Domain Models.

Contains entities related to semantic search, context enrichment, and retrieval.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict
from .entities import FactEntity

@dataclass
class EnrichedFact:
    """Fact enriched with search metadata."""
    fact_id: str
    content: str
    source: str  # "keyword", "phrase_1", "phrase_2"
    relevance_score: Optional[float] = None
    vector: Optional[List[float]] = None  # Included for semantic deduplication

@dataclass
class EnrichedContext:
    """Result of context enrichment process."""
    facts: List[EnrichedFact]
    total_sources: int
    dedup_count: int
    biographical_dedup_count: int  # Legacy field, kept for compatibility

@dataclass(frozen=True)
class SearchLimits:
    """
    Configuration for search limits overrides.
    
    Allows callers (e.g., ConsolidationAgent) to override default limits
    defined in SearchEnrichmentService.
    """
    keyword_limit: int
    phrase_one_limit: int
    phrase_two_limit: int
    total_limit: int

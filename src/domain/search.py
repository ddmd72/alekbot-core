"""
Search Domain Models.

Contains entities related to semantic search, context enrichment, and retrieval.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .entities import ExpiryKind
from .fact_expiry import expiry_view
from .fact_write_context import as_utc

@dataclass
class EnrichedFact:
    """Fact enriched with search metadata."""
    fact_id: str
    content: str
    source: str  # "keyword", "phrase_1", "phrase_2"
    relevance_score: Optional[float] = None
    vector: Optional[List[float]] = None  # Included for semantic deduplication
    # Taxonomy fields — populated by SearchEnrichmentService; None for callers that don't need them
    fact_type: Optional[str] = None
    domain: Optional[str] = None
    temporal_class: Optional[str] = None
    state: Optional[str] = None
    context_priority: Optional[str] = None
    tags: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None
    reported_date: Optional[str] = None  # ISO string
    context: Optional[str] = None
    version: Optional[int] = None
    # The stored expiry decision, raw. `expires_at` is an aware-UTC ISO string like `reported_date`: this object is
    # serialised with the agent context, which a datetime would break. Read it through `expiry_decision`.
    expiry_kind: Optional[ExpiryKind] = None
    ttl_days: Optional[int] = None
    expires_at: Optional[str] = None

    def expiry_decision(self, timezone: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """The stored decision as the model sees it (`expiry_view`); the one place `expires_at` is read back to a time."""
        expires = as_utc(self.expires_at) if self.expires_at is not None else None
        return expiry_view(self.expiry_kind, self.ttl_days, expires, timezone)

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

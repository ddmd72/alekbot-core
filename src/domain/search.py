"""
Search Domain Models.

Contains entities related to semantic search, context enrichment, and retrieval.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .entities import ExpiryKind, FactEntity
from .fact_expiry import expiry_view
from .fact_write_context import as_utc
from .local_time import local_moment

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

    @classmethod
    def from_fact(cls, fact: FactEntity, source: str, relevance_score: Optional[float]) -> "EnrichedFact":
        """The search view of a stored fact: taxonomy as strings, `reported_date` and `expires_at` as aware-UTC ISO."""
        expires_at = local_moment(fact.expires_at, "UTC").isoformat() if fact.expires_at is not None else None
        return cls(
            fact_id=fact.id,
            content=fact.text,
            vector=fact.vector,
            source=source,
            relevance_score=relevance_score,
            fact_type=fact.type.value if fact.type else None,
            domain=fact.domain.value if fact.domain else None,
            temporal_class=fact.temporal_class.value if fact.temporal_class else None,
            state=fact.state.value if fact.state else None,
            context_priority=fact.context_priority.value if fact.context_priority else None,
            tags=fact.tags,
            metadata=fact.metadata,
            reported_date=fact.reported_date.isoformat() if fact.reported_date else None,
            context=fact.context,
            version=fact.version,
            expiry_kind=fact.expiry_kind,
            ttl_days=fact.ttl_days,
            expires_at=expires_at,
        )

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


def _in_owner_zone(stamp: Optional[str], zone: str) -> Optional[str]:
    """An ISO timestamp as an ISO timestamp with the owner's offset; text that is not ISO is returned as stored."""
    if not stamp:
        return stamp
    try:
        return local_moment(as_utc(stamp), zone).isoformat(timespec="seconds")
    except ValueError:
        return stamp


def consolidation_hit(fact: EnrichedFact, timezone: str) -> Dict[str, Any]:
    """One search hit as the consolidator's `search_existing_facts` returns it, dates in the owner's zone."""
    expiry, due = fact.expiry_decision(timezone)
    return {
        "fact_id": fact.fact_id,
        "content": fact.content,
        "similarity": fact.relevance_score,
        "source": fact.source,
        "type": fact.fact_type,
        "domain": fact.domain,
        "temporal_class": fact.temporal_class,
        "state": fact.state,
        "context_priority": fact.context_priority,
        "tags": fact.tags or [],
        "metadata": fact.metadata or {},
        "reported_date": _in_owner_zone(fact.reported_date, timezone),
        "expiry": expiry,
        "due": due,
        "version": fact.version,
    }

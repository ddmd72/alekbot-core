"""
Search Domain Models.

Contains entities related to semantic search, context enrichment, and retrieval.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from .entities import ExpiryKind, FactEntity
from .fact_expiry import expiry_view
from .fact_write_context import as_utc
from .local_time import local_date, local_moment

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
    reported_date: Optional[datetime] = None  # aware UTC
    context: Optional[str] = None
    version: Optional[int] = None
    # The stored expiry decision, as stored: `expires_at` aware UTC. `fact_view` shows it to the model.
    expiry_kind: Optional[ExpiryKind] = None
    ttl_days: Optional[int] = None
    expires_at: Optional[datetime] = None

    @classmethod
    def from_fact(cls, fact: FactEntity, source: str, relevance_score: Optional[float]) -> "EnrichedFact":
        """The search view of a stored fact: taxonomy as strings, `reported_date` and `expires_at` aware UTC."""
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
            reported_date=as_utc(fact.reported_date) if fact.reported_date else None,
            context=fact.context,
            version=fact.version,
            expiry_kind=fact.expiry_kind,
            ttl_days=fact.ttl_days,
            expires_at=as_utc(fact.expires_at) if fact.expires_at is not None else None,
        )

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


def fact_view(fact: Union[FactEntity, EnrichedFact], timezone: str) -> Dict[str, Any]:
    """A stored fact as the consolidator sees it, from the document or from its search hit (which carries the same
    stored fields): `fact_id`, `content`, the decision as `expiry` (the shape the model sends; null when none is recorded)
    and `due` (the local day a horizon falls due), `reported_date` (the moment, with the owner's offset) and `reported`
    (the owner's calendar day of it)."""
    if isinstance(fact, EnrichedFact):
        fact_id, content = fact.fact_id, fact.content
    else:
        fact_id, content = fact.id, fact.text
    expiry, due = expiry_view(fact.expiry_kind, fact.ttl_days, fact.expires_at, timezone)
    reported = fact.reported_date
    return {
        "fact_id": fact_id,
        "content": content,
        "expiry": expiry,
        "due": due,
        "reported_date": local_moment(reported, timezone).isoformat(timespec="seconds") if reported else None,
        "reported": local_date(reported, timezone).isoformat() if reported else None,
    }


def consolidation_hit(fact: EnrichedFact, timezone: str) -> Dict[str, Any]:
    """One search hit as the consolidator's `search_existing_facts` returns it: the search's own fields and the fact's
    `fact_view`, dates in the owner's zone."""
    view = fact_view(fact, timezone)
    return {
        "fact_id": view["fact_id"],
        "content": view["content"],
        "similarity": fact.relevance_score,
        "source": fact.source,
        "type": fact.fact_type,
        "domain": fact.domain,
        "temporal_class": fact.temporal_class,
        "state": fact.state,
        "context_priority": fact.context_priority,
        "tags": fact.tags or [],
        "metadata": fact.metadata or {},
        "reported_date": view["reported_date"],
        "reported": view["reported"],
        "expiry": view["expiry"],
        "due": view["due"],
        "version": fact.version,
    }

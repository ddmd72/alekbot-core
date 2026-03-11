from enum import Enum
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
from uuid import uuid4
from pydantic import BaseModel, Field

class FactType(str, Enum):
    STATE = "state"         # Temporary state (weight, health indicators)
    EVENT = "event"         # Immutable event (biographical fact)
    PRINCIPLE = "principle" # Anchor / Belief (from anchors.yaml)
    SYSTEM = "system"       # Instructions and prompts (from .groovy files)
    ALERT = "alert"         # Critical alerts / Risks


# ========================================================================
# Deliberate Fact Management (RFC 2026-02-16): 4D Fact Taxonomy
# ========================================================================
class FactDomain(str, Enum):
    """WHAT is this fact about? (Structural category)"""
    BIOGRAPHICAL = "biographical"
    POSSESSION = "possession"
    HEALTH = "health"
    MEDICAL_RECORDS = "medical_records"
    LOCATION = "location"
    WORK = "work"
    NETWORK = "network"
    PREFERENCE = "preference"
    SKILL = "skill"
    PROJECT = "project"
    FINANCE = "finance"
    EDUCATION = "education"
    LEGAL = "legal"
    ENTERTAINMENT = "entertainment"
    COMMUNICATION = "communication"


class TemporalClass(str, Enum):
    """HOW LONG does this fact live? (Lifecycle category)"""
    PERMANENT = "permanent"
    STABLE = "stable"
    DYNAMIC = "dynamic"
    EPHEMERAL = "ephemeral"


class FactState(str, Enum):
    """IS this fact current/stale/archived? (Actuality status)"""
    CURRENT = "current"
    STALE = "stale"
    ARCHIVED = "archived"
    SUPERSEDED = "superseded"
    INVALIDATED = "invalidated"


class ContextPriority(str, Enum):
    """HOW IMPORTANT is this fact for baseline context?"""
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    ARCHIVAL = "archival"


# ========================================================================
# OAuth Multi-Tenant Session 1: Fact visibility control
# RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
# Purpose: Dual ownership model - facts owned by account, created by user
# ========================================================================
class FactVisibility(str, Enum):
    ACCOUNT_SHARED = "account_shared"  # Visible to all account members
    USER_PRIVATE = "user_private"      # Visible only to creator


# ========================================================================
# ARCHITECTURE FIX: Domain-level normalization for LLM → enum mapping.
# LLM sometimes returns "CURRENT" / "BIOGRAPHICAL" instead of lowercase.
# This is a DOMAIN concern (mapping raw output to FactDomain/FactState/etc.),
# not an adapter concern. Previously duplicated 6 times across 2 adapters.
# ========================================================================
_FACT_TAXONOMY_FIELDS = ("domain", "temporal_class", "state", "context_priority")


def normalize_fact_taxonomy(metadata: dict) -> dict:
    """Normalize 4D taxonomy fields to lowercase for enum compatibility.

    Call this BEFORE constructing FactEntity from LLM output.
    Returns a new dict — does not mutate the input.
    """
    result = dict(metadata)
    for field in _FACT_TAXONOMY_FIELDS:
        value = result.get(field)
        if value and isinstance(value, str):
            result[field] = value.lower()
    return result


class FactEntity(BaseModel):
    # --- Identifiers ---
    id: str = Field(default_factory=lambda: str(uuid4()))

    # ========================================================================
    # OAuth Multi-Tenant Session 1: Dual ownership model
    # RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
    # BREAKING CHANGE: owner_id renamed to account_id (billing entity)
    # Purpose: Separate billing owner from creator for multi-user accounts
    # ========================================================================
    account_id: str  # Billing account owner (tenant in multi-tenant architecture)
    created_by_user_id: str  # User who created this fact (for attribution and user-private visibility)
    lineage_id: str  # Links all versions of one fact (SCD2)

    # ========================================================================
    # REMOVED OAuth Multi-Tenant Session 1: Replaced by dual ownership
    # RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
    # ========================================================================
    # owner_id: str  # → Split into account_id (billing) + created_by_user_id (attribution)

    # --- Content ---
    text: str # Main text for semantic search
    vector: Optional[List[float]] = None # Embedding (stored in Firestore or Vector Search)
    
    # ========================================================================
    # SESSION 2026-02-07: Multi-Vector Search Support
    # RFC: docs/10_rfcs/BIOGRAPHICAL_CACHE_MULTI_VECTOR_RFC.md
    # Purpose: Enable multi-vector RRF search for biographical cache
    # ========================================================================
    tags_vector: Optional[List[float]] = None  # Domain keywords embedding (for biographical cache)
    metadata_vector: Optional[List[float]] = None  # Structured data embedding (for biographical cache)
    
    tags: List[str] = []
    type: FactType

    # --- Metadata (Structured data) ---
    metadata: Dict[str, Any] = {}

    # ========================================================================
    # Deliberate Fact Management: 4D Taxonomy (optional during transition)
    # ========================================================================
    domain: Optional[FactDomain] = None
    temporal_class: Optional[TemporalClass] = None
    state: FactState = FactState.CURRENT
    context_priority: ContextPriority = ContextPriority.MEDIUM

    # ========================================================================
    # Deliberate Fact Management: Explicit lifecycle tracking
    # ========================================================================
    ttl_days: Optional[int] = None
    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: Optional[datetime] = None

    # ========================================================================
    # Deliberate Fact Management: Explicit version tracking
    # ========================================================================
    version: int = 1
    replaces_fact_id: Optional[str] = None

    # ========================================================================
    # Deliberate Fact Management: Context metadata
    # ========================================================================
    context: Optional[str] = None
    reported_date: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # --- SCD Type 2 (Time Travel) ---
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    valid_from: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    valid_to: Optional[datetime] = None  # None = current truth
    is_current: bool = True

    # --- Access ---
    # ========================================================================
    # OAuth Multi-Tenant Session 1: Typed visibility control
    # RFC: docs/10_rfcs/MULTI_TENANT_OAUTH_RFC.md
    # BREAKING CHANGE: visibility changed from str to enum
    # ========================================================================
    visibility: FactVisibility = FactVisibility.ACCOUNT_SHARED  # Default: shared with account members

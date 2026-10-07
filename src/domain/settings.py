# ========================================================================
# ARCHITECTURE FIX: Moved from src/config/settings.py to src/domain/.
# SearchConfig contains domain-level constants (search limits, biographical
# cache sizes, tiered defaults). Services import from domain/ and ports/
# only — importing from config/ violated the hexagonal import rule.
#
# ConsolidationSettings: same pattern — pure value object with int/str
# defaults. Moved from config/settings.py (2026-03-08) so that handlers/
# can import it without violating the hexagonal config/ boundary.
# ========================================================================
from dataclasses import dataclass, field
from typing import Dict, List

from .billing import AccountTier


@dataclass
class ConsolidationSettings:
    """Centralized settings for sliding window consolidation."""
    threshold: int = 70  # dev default
    batch_size: int = 50  # dev default
    max_queue_messages: int = 600
    max_retry_attempts: int = 3
    prompt_version: str = "v3"  # "v3" (multi-turn deliberate) or "v2" (legacy single-shot)


@dataclass
class SearchConfig:
    """
    Centralized settings for semantic search (multi-vector).

    Session: 2026-02-07 Multi-Vector Semantic Search
    Plan: docs/SESSION_2026_02_07_MULTI_VECTOR_SEMANTIC_SEARCH.md
    Purpose: System-wide defaults for search context limits
    """
    # Semantic search (SearchEnrichmentService) defaults
    DEFAULT_SEMANTIC_SEARCH_LIMIT: int = 30
    DEFAULT_KEYWORD_LIMIT: int = 10
    DEFAULT_PHRASE_ONE_LIMIT: int = 10
    DEFAULT_PHRASE_TWO_LIMIT: int = 10

    # Memory search (MemorySearchAgent) - future use
    DEFAULT_MEMORY_SEARCH_LIMIT: int = 50

    # Biographical cache (BiographicalContextService) defaults
    # Session: 2026-02-07 Biographical Cache Optimization
    # Plan: docs/SESSION_2026_02_07_BIOGRAPHICAL_CACHE_OPTIMIZATION.md
    # RFC: docs/10_rfcs/BIOGRAPHICAL_CACHE_MULTI_VECTOR_RFC.md
    DEFAULT_BIOGRAPHICAL_CACHE_LIMIT: int = 65
    DEFAULT_PRINCIPLES_CACHE_LIMIT: int = 20
    # Standing directives (agent_directive domain, STANDING_DIRECTIVES_RFC):
    # injection bound = the cap. Consolidator curates the rulebook to <=15 in Stage 2b and a
    # deterministic code backstop enforces the same 15 in storage; this bound guarantees the
    # orchestrator prompt can never exceed it, whatever the rulebook state.
    DEFAULT_DIRECTIVES_CACHE_LIMIT: int = 15

    # History optimization (2026-02-18): Tiered history loading
    DEFAULT_HISTORY_RECENT_FULL_TURNS: int = 2

    # Default queries for biographical cache multi-vector search
    DEFAULT_BIOGRAPHICAL_QUERIES: List[str] = field(default_factory=lambda: [
        "identity name bio family relationships",  # Personal identity
        "medical health conditions diagnoses",     # Health facts
        "assets possessions vehicles property",    # Material facts
    ])

    # ========================================================================
    # NEW Biographical Keywords (2026-02-07): Configurable query keywords
    # Plan: docs/SESSION_2026_02_07_BIOGRAPHICAL_CACHE_REFACTORING.md
    # Purpose: 3 separate keyword sets for multi-vector biographical search
    # ========================================================================
    DEFAULT_BIO_KEYWORDS_QUERY1: List[str] = field(default_factory=lambda: [  # Query 1: tags + metadata
        "identity", "name", "bio", "family", "relationships"
    ])
    DEFAULT_BIO_KEYWORDS_QUERY2: List[str] = field(default_factory=lambda: [  # Query 2: vector + tags
        "medical", "health", "conditions", "diagnoses", "treatments"
    ])
    DEFAULT_BIO_KEYWORDS_QUERY3: List[str] = field(default_factory=lambda: [  # Query 3: vector + metadata
        "assets", "possessions", "vehicles", "property", "finances"
    ])

    # Tiered defaults (can be overridden at account level)
    # These are optional defaults - account owners can set custom limits
    TIERED_SEMANTIC_LIMITS: Dict[AccountTier, int] = field(default_factory=lambda: {
        AccountTier.FREE: 20,       # Budget-conscious
        AccountTier.FAMILY: 30,     # Standard quality
        AccountTier.PRO: 50,        # Higher quality
        AccountTier.ENTERPRISE: 100  # Maximum recall
    })
    TIERED_BIOGRAPHICAL_LIMITS: Dict[AccountTier, int] = field(default_factory=lambda: {
        AccountTier.FREE: 30,       # Budget-conscious
        AccountTier.FAMILY: 50,     # Standard quality
        AccountTier.PRO: 70,        # Higher quality
        AccountTier.ENTERPRISE: 100  # Maximum recall
    })
    TIERED_PRINCIPLES_LIMITS: Dict[AccountTier, int] = field(default_factory=lambda: {
        AccountTier.FREE: 10,       # Budget-conscious
        AccountTier.FAMILY: 15,     # Standard quality
        AccountTier.PRO: 20,        # Higher quality
        AccountTier.ENTERPRISE: 25  # Maximum recall
    })

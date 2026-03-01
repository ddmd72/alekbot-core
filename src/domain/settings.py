# ========================================================================
# ARCHITECTURE FIX: Moved from src/config/settings.py to src/domain/.
# SearchConfig contains domain-level constants (search limits, biographical
# cache sizes, tiered defaults). Services import from domain/ and ports/
# only — importing from config/ violated the hexagonal import rule.
# ========================================================================
from dataclasses import dataclass


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

    # History optimization (2026-02-18): Tiered history loading
    DEFAULT_HISTORY_RECENT_FULL_TURNS: int = 5

    # Default queries for biographical cache multi-vector search
    DEFAULT_BIOGRAPHICAL_QUERIES: list = None

    # ========================================================================
    # NEW Biographical Keywords (2026-02-07): Configurable query keywords
    # Plan: docs/SESSION_2026_02_07_BIOGRAPHICAL_CACHE_REFACTORING.md
    # Purpose: 3 separate keyword sets for multi-vector biographical search
    # ========================================================================
    DEFAULT_BIO_KEYWORDS_QUERY1: list = None  # Query 1: tags + metadata
    DEFAULT_BIO_KEYWORDS_QUERY2: list = None  # Query 2: vector + tags
    DEFAULT_BIO_KEYWORDS_QUERY3: list = None  # Query 3: vector + metadata

    # Tiered defaults (can be overridden at account level)
    # These are optional defaults - account owners can set custom limits
    TIERED_SEMANTIC_LIMITS: dict = None
    TIERED_BIOGRAPHICAL_LIMITS: dict = None
    TIERED_PRINCIPLES_LIMITS: dict = None

    def __post_init__(self):
        """Initialize tiered limits and default queries if not provided."""
        from .billing import AccountTier

        if self.TIERED_SEMANTIC_LIMITS is None:
            self.TIERED_SEMANTIC_LIMITS = {
                AccountTier.FREE: 20,       # Budget-conscious
                AccountTier.FAMILY: 30,     # Standard quality
                AccountTier.PRO: 50,        # Higher quality
                AccountTier.ENTERPRISE: 100 # Maximum recall
            }

        if self.TIERED_BIOGRAPHICAL_LIMITS is None:
            self.TIERED_BIOGRAPHICAL_LIMITS = {
                AccountTier.FREE: 30,       # Budget-conscious
                AccountTier.FAMILY: 50,     # Standard quality
                AccountTier.PRO: 70,        # Higher quality
                AccountTier.ENTERPRISE: 100 # Maximum recall
            }

        if self.TIERED_PRINCIPLES_LIMITS is None:
            self.TIERED_PRINCIPLES_LIMITS = {
                AccountTier.FREE: 10,       # Budget-conscious
                AccountTier.FAMILY: 15,     # Standard quality
                AccountTier.PRO: 20,        # Higher quality
                AccountTier.ENTERPRISE: 25  # Maximum recall
            }

        if self.DEFAULT_BIOGRAPHICAL_QUERIES is None:
            self.DEFAULT_BIOGRAPHICAL_QUERIES = [
                "identity name bio family relationships",  # Personal identity
                "medical health conditions diagnoses",     # Health facts
                "assets possessions vehicles property",    # Material facts
            ]

        # ========================================================================
        # NEW Biographical Keywords (2026-02-07): Initialize keyword sets
        # ========================================================================
        if self.DEFAULT_BIO_KEYWORDS_QUERY1 is None:
            self.DEFAULT_BIO_KEYWORDS_QUERY1 = [
                "identity", "name", "bio", "family", "relationships"
            ]

        if self.DEFAULT_BIO_KEYWORDS_QUERY2 is None:
            self.DEFAULT_BIO_KEYWORDS_QUERY2 = [
                "medical", "health", "conditions", "diagnoses", "treatments"
            ]

        if self.DEFAULT_BIO_KEYWORDS_QUERY3 is None:
            self.DEFAULT_BIO_KEYWORDS_QUERY3 = [
                "assets", "possessions", "vehicles", "property", "finances"
            ]

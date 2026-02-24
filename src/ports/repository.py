from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any, Tuple
from ..domain.entities import FactEntity

class FactRepository(ABC):
    """
    Abstract Port for Fact storage and retrieval.
    Follows Hexagonal Architecture principles.
    """

    @abstractmethod
    async def add_fact(self, fact: FactEntity) -> str:
        """Adds a new fact to the repository."""
        pass

    @abstractmethod
    async def get_fact_by_id(self, fact_id: str) -> Optional[FactEntity]:
        """Retrieves a fact by its ID."""
        pass

    @abstractmethod
    async def get_facts_by_ids(self, fact_ids: List[str]) -> List[FactEntity]:
        """Retrieves multiple facts by their IDs. Missing facts are omitted from results."""
        pass

    @abstractmethod
    async def get_active_facts(self, owner_id: str, tags: Optional[List[str]] = None) -> List[FactEntity]:
        """Retrieves all current (is_current=True) facts for a specific owner."""
        pass

    @abstractmethod
    async def get_active_facts_ordered(
        self,
        account_id: str,
        domain: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[FactEntity]:
        """
        Current non-archival facts ordered by priority rank then recency.

        Args:
            account_id: Account ID
            domain: Optional domain filter (e.g. "biographical")
            limit: Maximum number of results

        Returns:
            Facts sorted by context_priority_rank ASC, created_at DESC.
            Excludes facts with ARCHIVAL priority.
        """
        pass

    @abstractmethod
    async def get_paginated_facts(
        self,
        owner_id: str,
        limit: int = 100,
        cursor_doc_id: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> Tuple[List[FactEntity], Optional[str]]:
        """
        Cursor-based paginated retrieval of current facts for a specific owner.

        Args:
            owner_id: Account ID
            limit: Page size (max results returned)
            cursor_doc_id: Firestore document ID of the last item from the previous page
            domain: Optional domain filter (e.g. "health", "location")

        Returns:
            Tuple of (facts, next_cursor_doc_id).
            next_cursor_doc_id is None when there are no more pages.
        """
        pass

    @abstractmethod
    async def search_facts(
        self,
        query_vector: List[float],
        limit: int = 5,
        user_id: Optional[str] = None,
        account_id: Optional[str] = None
    ) -> List[FactEntity]:
        """
        Performs a vector search for facts.

        Multi-tenant resolution (Session 27):
        - If account_id/user_id are NOT passed → taken from RequestContext
        - Default: searches facts by account_id from context
        - Explicit parameters → override (for searching on behalf of other users)

        Args:
            query_vector: Query embedding vector
            limit: Maximum results
            user_id: Explicit user ID override (optional, from context if None)
            account_id: Explicit account ID override (optional, from context if None)

        Returns:
            List of relevant facts sorted by similarity

        Raises:
            ValueError: If context is not set and parameters are not passed

        Related: RFC REQUEST_CONTEXT_RFC.md
        """
        pass

    @abstractmethod
    async def update_fact(self, fact: FactEntity) -> None:
        """Updates an existing fact (usually for SCD2 versioning)."""
        pass

    @abstractmethod
    async def get_lineage(self, lineage_id: str) -> List[FactEntity]:
        """Retrieves the full history of a fact lineage."""
        pass

    @abstractmethod
    async def get_latest_fact_by_lineage(self, owner_id: str, lineage_id: str) -> Optional[FactEntity]:
        """Retrieves the latest version of a fact lineage for a specific owner."""
        pass

    @abstractmethod
    async def add_observation(self, observation: Dict[str, Any], owner_id: str) -> None:
        """Adds a raw observation for later consolidation."""
        pass

    @abstractmethod
    async def get_observations(self, owner_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Retrieves raw observations for a specific owner."""
        pass

    @abstractmethod
    async def archive_observations(self, observation_ids: List[str], owner_id: str) -> None:
        """Moves observations to an archive collection."""
        pass

    @abstractmethod
    async def add_fact_if_unique(
        self, 
        fact: FactEntity, 
        similarity_threshold: float = 0.85
    ) -> tuple[bool, Optional[str]]:
        """
        Add fact only if no semantic duplicate exists.
        
        Args:
            fact: FactEntity to add
            similarity_threshold: Threshold for semantic similarity
            
        Returns:
            tuple (was_added, duplicate_id)
        """
        pass

    @abstractmethod
    async def get_biographical_context(
        self, 
        owner_id: str, 
        limit: int = 100
    ) -> List[FactEntity]:
        """
        Retrieves biographical context for consolidation (semantic search).
        
        Args:
            owner_id: User ID
            limit: Maximum number of context facts
            
        Returns:
            List of relevant facts
        """
        pass

    @abstractmethod
    async def refresh_biographical_context_cache(
        self, 
        owner_id: str,
        facts_limit: Optional[int] = None,
        principles_limit: Optional[int] = None
    ) -> None:
        """
        Refresh cached biographical context after consolidation.
        
        Session: 2026-02-07 Biographical Cache Optimization
        Purpose: Support configurable limits (USER → ACCOUNT → SYSTEM resolution)
        
        Args:
            owner_id: Account ID
            facts_limit: Max biographical facts (None = use system default)
            principles_limit: Max principles (None = use system default)
        """
        pass

    @abstractmethod
    async def get_biographical_context_cached(
        self, 
        owner_id: str, 
        limit: int = 100
    ) -> List[Dict]:
        """
        Retrieves cached biographical context (fast read).
        
        Args:
            owner_id: User ID
            limit: Maximum number of context facts
            
        Returns:
            List of cached fact dictionaries
        """
        pass

    @abstractmethod
    async def invalidate_fact(self, fact_id: str, account_id: str) -> None:
        """
        Directly mark a fact as invalidated (User Cabinet write path).

        Security: account_id is verified server-side against the document.
        Raises PermissionError if account_id does not match the fact owner.
        Raises ValueError if the fact does not exist.
        """
        pass

    @abstractmethod
    async def get_legacy_facts(
        self,
        account_id: str,
        limit: int = 20
    ) -> List[FactEntity]:
        """
        Retrieves legacy facts (missing domain taxonomy) for migration.
        
        Session: 2026-02-17 Legacy Fact Migration
        Purpose: Support deliberate reclassification through ConsolidationAgent v3
        
        Args:
            account_id: Account ID to migrate
            limit: Maximum number of facts to return (default: 20)
            
        Returns:
            List of legacy facts ordered by created_at ASC (oldest first)
        """
        pass

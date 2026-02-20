import asyncio
import datetime
from typing import List, Optional, Dict, Any, Tuple
from google.cloud import firestore
from google.cloud.firestore import FieldFilter
from google.cloud.firestore_v1.vector import Vector
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from ..domain.entities import FactEntity, FactType
from ..domain.vector_math import cosine_similarity
from ..domain.deduplication_service import SmartDeduplicationService
from ..ports.repository import FactRepository
from ..config.environment import EnvironmentConfig
from ..utils.timer import log_execution_time
from ..ports.embedding_service import EmbeddingService
from ..adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
from ..utils.logger import logger

class FirestoreFactRepository(FactRepository):
    """
    Adapter for Google Cloud Firestore with environment isolation.
    Implements SCD Type 2 and Vector Search support.
    """

    def __init__(
        self,
        db_client,
        env_config: EnvironmentConfig,
        embedding_service: Optional[EmbeddingService] = None,
        biographical_context_service: Optional["BiographicalContextService"] = None,
        dedup_service: Optional[SmartDeduplicationService] = None,
    ):
        """
        Initialize repository with database client and environment config.

        Session 2026-02-07: Hexagonal Architecture Refactoring
        Added biographical_context_service for DI - prevents Infrastructure→Application dependency.
        Session 2026-02-20: Added dedup_service via DI (removed lazy service import).

        Args:
            db_client: Firestore AsyncClient instance
            env_config: Environment configuration for collection isolation
            embedding_service: Service for generating embeddings (DI)
            biographical_context_service: Service for biographical cache refresh (DI, NEW)
            dedup_service: Smart deduplication logic (DI; defaults to SmartDeduplicationService())
        """
        self.db = db_client
        self.env_config = env_config
        self._embedding_service = embedding_service
        self._biographical_context_service = biographical_context_service
        self._dedup_service = dedup_service or SmartDeduplicationService()

        # Environment-aware collection names (ADR-006 Semantic Naming)
        # Note: observations are legacy/deprecated, keeping logic for now but marking as such
        prefix = env_config.firestore_collection_prefix
        
        # ADR-006: Use semantic domain_facts_v2
        self.facts_col = self.db.collection(env_config.domain_facts_collection)
        
        # Legacy/Infra collections
        self.obs_col = self.db.collection(f"{prefix}observations_deprecated")
        self.obs_archive_col = self.db.collection(f"{prefix}observations_archive_deprecated")
        
        # ADR-006: Infra collection (stable name)
        self.user_context_col = self.db.collection(env_config.user_context_collection)
        
        # Pre-computed biographical query vector
        self._bio_query_vector: Optional[List[float]] = None

        logger.info(f"📂 Firestore collections: {env_config.domain_facts_collection}, {env_config.user_context_collection}")

        # Production safety warnings
        if env_config.is_production:
            logger.warning("🔒 PRODUCTION MODE: Destructive operations blocked")

    async def initialize(self) -> None:
        """Initialize async components and pre-compute embeddings."""
        if not self._embedding_service:
            from ..config.settings import load_settings
            settings = load_settings()
            api_key = settings.get("GEMINI_API_KEY")
            self._embedding_service = GeminiEmbeddingAdapter(api_key=api_key)

        if not self._bio_query_vector:
            query = "name bio family assets relationships beliefs"
            self._bio_query_vector = await self._embedding_service.get_embedding(query)
            logger.info("✅ Biographical query embedding pre-computed and cached")

    def _migrate_ownership_fields(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Migrate legacy owner_id field to new OAuth multi-tenant fields.

        OAuth Multi-Tenant Session 8: Backward compatibility for unmigrated data.
        - Old schema: owner_id (single field)
        - New schema: account_id (billing entity) + created_by_user_id (attribution)

        Args:
            data: Raw Firestore document data

        Returns:
            Migrated data with new fields
        """
        # If new fields already exist, no migration needed
        if "account_id" in data and "created_by_user_id" in data:
            return data

        # If old owner_id exists, migrate it
        if "owner_id" in data:
            owner_id = data.pop("owner_id")
            # For backward compatibility, use owner_id for both fields
            data["account_id"] = owner_id
            data["created_by_user_id"] = owner_id
            logger.debug(f"Migrated legacy owner_id={owner_id} to account_id/created_by_user_id")

        # Ensure visibility field exists and migrate old values
        if "visibility" not in data:
            from ..domain.entities import FactVisibility
            data["visibility"] = FactVisibility.ACCOUNT_SHARED.value
        elif data["visibility"] == "private":
            # Migrate old 'private' value to new enum 'user_private'
            from ..domain.entities import FactVisibility
            data["visibility"] = FactVisibility.USER_PRIVATE.value
            logger.debug(f"Migrated visibility from 'private' to 'user_private'")

        return data

    async def add_fact(self, fact: FactEntity) -> str:
        doc_ref = self.facts_col.document(fact.id)
        data = fact.model_dump()
        
        # SESSION 2026-02-09: FIX - Vector() wrapper REQUIRED for correct serialization
        # Without Vector() wrapper, Firestore saves as map {0: val, 1: val, ...} instead of array
        # Index type (FLAT vs HNSW) is controlled by firestore.indexes.json, NOT by wrapper
        # Vector() wrapper ensures proper vector field recognition by Firestore Vector Search
        
        if 'vector' in data and data['vector'] is not None:
            data['vector'] = Vector(data['vector'])
        if 'tags_vector' in data and data['tags_vector'] is not None:
            data['tags_vector'] = Vector(data['tags_vector'])
        if 'metadata_vector' in data and data['metadata_vector'] is not None:
            data['metadata_vector'] = Vector(data['metadata_vector'])
        
        await doc_ref.set(data)
        return fact.id

    async def get_fact_by_id(self, fact_id: str) -> Optional[FactEntity]:
        doc = await self.facts_col.document(fact_id).get()
        if doc.exists:
            data = doc.to_dict()
            # Convert Vector back to list if needed
            if 'vector' in data and isinstance(data['vector'], Vector):
                data['vector'] = list(data['vector'])
            # SESSION 2026-02-07: Multi-vector support
            if 'tags_vector' in data and isinstance(data['tags_vector'], Vector):
                data['tags_vector'] = list(data['tags_vector'])
            if 'metadata_vector' in data and isinstance(data['metadata_vector'], Vector):
                data['metadata_vector'] = list(data['metadata_vector'])
            # Backward compatibility: migrate old owner_id to new fields
            data = self._migrate_ownership_fields(data)
            return FactEntity(**data)
        return None

    async def get_facts_by_ids(self, fact_ids: List[str]) -> List[FactEntity]:
        """Parallel fetch of multiple facts by ID. Missing facts are omitted."""
        if not fact_ids:
            return []
        results = await asyncio.gather(
            *[self.get_fact_by_id(fid) for fid in fact_ids]
        )
        return [f for f in results if f is not None]

    @log_execution_time
    async def get_active_facts(self, owner_id: str, tags: Optional[List[str]] = None) -> List[FactEntity]:
        """
        Get active facts by account_id (owner_id parameter kept for backward compatibility).

        SESSION 2026-02-16: Filter SUPERSEDED facts (not is_current - obsolete legacy field)
        Removed fallback to owner_id - all facts should have account_id after migration.
        Used by BiographicalContextService for cache refresh.

        Args:
            owner_id: Account ID (parameter name kept for backward compatibility)
            tags: Optional list of tags to filter by
        """
        # SESSION 2026-02-17: Query by account_id + state == current
        # Only CURRENT facts (not STALE, ARCHIVED, SUPERSEDED, INVALIDATED)
        # Index: (account_id, state, __name__) - already created
        query = self.facts_col.where(filter=FieldFilter("account_id", "==", owner_id)).where(filter=FieldFilter("state", "==", "current"))

        if tags:
            # Firestore supports 'array_contains_any' for tags
            query = query.where(filter=FieldFilter("tags", "array_contains_any", tags))

        # Use get() instead of stream() for better performance on small result sets
        docs = await query.get()

        results = []
        for doc in docs:
            data = doc.to_dict()
            if 'vector' in data and isinstance(data['vector'], Vector):
                data['vector'] = list(data['vector'])
            # SESSION 2026-02-07: Multi-vector support
            if 'tags_vector' in data and isinstance(data['tags_vector'], Vector):
                data['tags_vector'] = list(data['tags_vector'])
            if 'metadata_vector' in data and isinstance(data['metadata_vector'], Vector):
                data['metadata_vector'] = list(data['metadata_vector'])
            # Migrate ownership fields for backward compatibility
            data = self._migrate_ownership_fields(data)
            results.append(FactEntity(**data))
        return results

    @log_execution_time
    async def get_paginated_facts(
        self,
        owner_id: str,
        limit: int = 100,
        cursor_doc_id: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> Tuple[List[FactEntity], Optional[str]]:
        """
        Cursor-based paginated retrieval ordered by created_at DESC.

        Fetches limit+1 docs to detect whether a next page exists without
        a separate COUNT query. The extra doc is never returned to the caller.

        Requires Firestore composite indexes:
          (account_id, state, created_at DESC)
          (account_id, state, domain, created_at DESC)  — when domain filter used
        """
        query = (
            self.facts_col
            .where(filter=FieldFilter("account_id", "==", owner_id))
            .where(filter=FieldFilter("state", "==", "current"))
            .order_by("created_at", direction=firestore.Query.DESCENDING)
        )

        if domain:
            query = query.where(filter=FieldFilter("domain", "==", domain))

        if cursor_doc_id:
            cursor_doc = await self.facts_col.document(cursor_doc_id).get()
            if cursor_doc.exists:
                query = query.start_after(cursor_doc)

        query = query.limit(limit + 1)
        docs = await query.get()

        has_more = len(docs) > limit
        page_docs = docs[:limit]

        results = []
        for doc in page_docs:
            data = doc.to_dict()
            for field in ("vector", "tags_vector", "metadata_vector"):
                if field in data and isinstance(data[field], Vector):
                    data[field] = list(data[field])
            data = self._migrate_ownership_fields(data)
            results.append(FactEntity(**data))

        next_cursor = page_docs[-1].id if has_more and page_docs else None
        return results, next_cursor

    async def invalidate_fact(self, fact_id: str, account_id: str) -> None:
        """Directly mark a fact as invalidated. Security: account_id verified."""
        doc_ref = self.facts_col.document(fact_id)
        doc = await doc_ref.get()
        if not doc.exists:
            raise ValueError(f"Fact not found: {fact_id}")
        if doc.to_dict().get("account_id") != account_id:
            raise PermissionError("Not authorized to modify this fact")
        await doc_ref.update({"state": "invalidated"})

    @log_execution_time
    async def search_facts(
        self,
        query_vector: List[float],
        vector_field: str = "vector",
        limit: int = 10,
        user_id: Optional[str] = None,
        account_id: Optional[str] = None
    ) -> List[FactEntity]:
        """
        Performs a vector search in Firestore with implicit context resolution.

        Session 27: Multi-Tenant Request Context
        - If account_id/user_id are NOT passed → taken from RequestContext
        - Default: searches facts by account_id from context (priority!)
        - Explicit parameters → override (for searching on behalf of other users)

        Session 2026-02-07: Multi-Vector Search Support
        - Supports search across different vector fields (text, metadata, tags)
        - Backward compatible: default = "vector" (text embeddings)

        Args:
            query_vector: Query embedding vector
            vector_field: Vector field to search ("vector" | "metadata_vector" | "tags_vector")
            limit: Maximum number of results
            user_id: Explicit user ID override (optional, from context if None)
            account_id: Explicit account ID override (optional, from context if None)

        Returns:
            List of facts sorted by semantic similarity

        Raises:
            ValueError: If context is not set and parameters are not passed
        """
        # STEP 1: Resolve IDs (explicit or from context)
        from ..domain.request_context import get_current_user_id, get_current_account_id

        resolved_user_id = user_id or get_current_user_id()
        resolved_account_id = account_id or get_current_account_id()

        if not resolved_user_id:
            raise ValueError(
                "user_id not provided and no RequestContext set. "
                "Use RequestContext() in ConversationHandler."
            )

        # STEP 2: Multi-tenant logic - account_id takes priority!
        search_account_id = resolved_account_id or resolved_user_id

        logger.debug(
            f"🔍 [search_facts] Resolved: user_id={resolved_user_id[:8] if resolved_user_id else None}..., "
            f"account_id={resolved_account_id[:12] if resolved_account_id else None}..., "
            f"searching by={search_account_id[:12]}..."
        )

        # STEP 3: Vector search by account_id with configurable vector field
        # SESSION 2026-02-17: Only CURRENT facts (not STALE, ARCHIVED, SUPERSEDED, INVALIDATED)
        vector_query = (
            self.facts_col
            .where(filter=FieldFilter("account_id", "==", search_account_id))
            .where(filter=FieldFilter("state", "==", "current"))  # Only active current facts
            .find_nearest(
                vector_field=vector_field,  # Configurable: "vector" | "metadata_vector" | "tags_vector"
                query_vector=query_vector,  # REMOVED Vector() wrapper to avoid forcing FLAT index
                distance_measure=DistanceMeasure.COSINE,
                limit=limit
            )
        )

        docs = await vector_query.get()

        # STEP 5: Parse results
        results = []
        for doc in docs:
            data = doc.to_dict()
            if 'vector' in data and isinstance(data['vector'], Vector):
                data['vector'] = list(data['vector'])
            # SESSION 2026-02-07: Multi-vector support
            if 'tags_vector' in data and isinstance(data['tags_vector'], Vector):
                data['tags_vector'] = list(data['tags_vector'])
            if 'metadata_vector' in data and isinstance(data['metadata_vector'], Vector):
                data['metadata_vector'] = list(data['metadata_vector'])
            data = self._migrate_ownership_fields(data)
            results.append(FactEntity(**data))

        logger.debug(f"🔍 [search_facts] Found {len(results)} results")
        return results

    async def update_fact(self, fact: FactEntity) -> None:
        doc_ref = self.facts_col.document(fact.id)
        data = fact.model_dump()
        
        # SESSION 2026-02-09: FIX - Vector() wrapper REQUIRED for correct serialization
        # Without Vector() wrapper, Firestore saves as map {0: val, 1: val, ...} instead of array
        # Index type (FLAT vs HNSW) is controlled by firestore.indexes.json, NOT by wrapper
        # Vector() wrapper ensures proper vector field recognition by Firestore Vector Search
        
        if 'vector' in data and data['vector'] is not None:
            data['vector'] = Vector(data['vector'])
        if 'tags_vector' in data and data['tags_vector'] is not None:
            data['tags_vector'] = Vector(data['tags_vector'])
        if 'metadata_vector' in data and data['metadata_vector'] is not None:
            data['metadata_vector'] = Vector(data['metadata_vector'])
        
        await doc_ref.set(data)

    async def get_lineage(self, lineage_id: str) -> List[FactEntity]:
        docs = self.facts_col.where(filter=FieldFilter("lineage_id", "==", lineage_id)).order_by("created_at", direction=firestore.Query.DESCENDING).stream()
        results = []
        async for doc in docs:
            data = doc.to_dict()
            if 'vector' in data and isinstance(data['vector'], Vector):
                data['vector'] = list(data['vector'])
            # SESSION 2026-02-07: Multi-vector support
            if 'tags_vector' in data and isinstance(data['tags_vector'], Vector):
                data['tags_vector'] = list(data['tags_vector'])
            if 'metadata_vector' in data and isinstance(data['metadata_vector'], Vector):
                data['metadata_vector'] = list(data['metadata_vector'])
            results.append(FactEntity(**data))
        return results

    async def get_latest_fact_by_lineage(self, owner_id: str, lineage_id: str) -> Optional[FactEntity]:
        """Get latest fact by lineage for an account (owner_id = account_id)."""
        # Try new field first
        docs = self.facts_col.where(filter=FieldFilter("account_id", "==", owner_id)).where(filter=FieldFilter("lineage_id", "==", lineage_id)).where(filter=FieldFilter("is_current", "==", True)).limit(1).stream()
        async for doc in docs:
            data = doc.to_dict()
            if 'vector' in data and isinstance(data['vector'], Vector):
                data['vector'] = list(data['vector'])
            # SESSION 2026-02-07: Multi-vector support
            if 'tags_vector' in data and isinstance(data['tags_vector'], Vector):
                data['tags_vector'] = list(data['tags_vector'])
            if 'metadata_vector' in data and isinstance(data['metadata_vector'], Vector):
                data['metadata_vector'] = list(data['metadata_vector'])
            data = self._migrate_ownership_fields(data)
            return FactEntity(**data)

        # Fallback to legacy field
        docs = self.facts_col.where(filter=FieldFilter("owner_id", "==", owner_id)).where(filter=FieldFilter("lineage_id", "==", lineage_id)).where(filter=FieldFilter("is_current", "==", True)).limit(1).stream()
        async for doc in docs:
            data = doc.to_dict()
            if 'vector' in data and isinstance(data['vector'], Vector):
                data['vector'] = list(data['vector'])
            # SESSION 2026-02-07: Multi-vector support
            if 'tags_vector' in data and isinstance(data['tags_vector'], Vector):
                data['tags_vector'] = list(data['tags_vector'])
            if 'metadata_vector' in data and isinstance(data['metadata_vector'], Vector):
                data['metadata_vector'] = list(data['metadata_vector'])
            data = self._migrate_ownership_fields(data)
            return FactEntity(**data)

        return None

    async def add_observation(self, observation: Dict[str, Any], owner_id: str) -> None:
        """
        Add observation for a user/account.

        Args:
            observation: Observation data
            owner_id: Account ID (parameter name kept for backward compatibility)
        """
        doc_id = observation.get("id") or f"obs_{datetime.datetime.utcnow().timestamp()}"
        # Store as account_id for new schema, but keep owner_id for backward compatibility
        observation['account_id'] = owner_id
        observation['owner_id'] = owner_id  # Keep for backward compatibility
        await self.obs_col.document(doc_id).set(observation)

    @log_execution_time
    async def get_observations(self, owner_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Get observations for an account.

        Args:
            owner_id: Account ID (parameter name kept for backward compatibility)
        """
        # Try new field first
        query = self.obs_col.where(filter=FieldFilter("account_id", "==", owner_id)).limit(limit)
        docs = await query.get()

        # Fallback to legacy field if no results
        if not docs:
            query = self.obs_col.where(filter=FieldFilter("owner_id", "==", owner_id)).limit(limit)
            docs = await query.get()

        results = []
        for doc in docs:
            results.append(doc.to_dict())
        return results

    async def archive_observations(self, observation_ids: List[str], owner_id: str) -> None:
        """
        Archive observations for an account.

        Args:
            observation_ids: List of observation IDs to archive
            owner_id: Account ID (parameter name kept for backward compatibility)
        """
        # Production safety check
        self._check_production_safety("archive_observations")

        # Use Firestore Batch API for atomic operations
        batch = self.db.batch()
        archived_at = datetime.datetime.utcnow().isoformat()

        # First, read all observations to verify ownership
        observations_to_archive = []
        for obs_id in observation_ids:
            doc = await self.obs_col.document(obs_id).get()
            if doc.exists:
                data = doc.to_dict()
                # Verify ownership before archiving (check both new and old fields)
                account_id = data.get('account_id') or data.get('owner_id')
                if account_id == owner_id:
                    data['archived_at'] = archived_at
                    observations_to_archive.append((obs_id, data))
                else:
                    print(f"⚠️ Skipping archive for observation {obs_id}: Owner mismatch")

        # Batch write: copy to archive + delete from observations
        for obs_id, data in observations_to_archive:
            batch.set(self.obs_archive_col.document(obs_id), data)
            batch.delete(self.obs_col.document(obs_id))

        # Commit all operations atomically
        if observations_to_archive:
            await batch.commit()
            print(f"✅ Archived {len(observations_to_archive)} observations in batch")

    async def add_fact_if_unique(
        self, 
        fact: FactEntity, 
        similarity_threshold: float = 0.96
    ) -> tuple[bool, Optional[str]]:
        """
        Add fact only if no semantic duplicate exists using smart deduplication.
        
        SESSION 2026-02-08: Improved deduplication strategy
        - Raised threshold from 0.85 to 0.96 (less aggressive)
        - Number-aware comparison (sorted, order-independent)
        - Length-based heuristic for detail preservation
        - Philosophy: Better to add duplicate than lose important information
        
        Algorithm (SmartDeduplicationService):
        1. similarity < 0.96 → NOT duplicate
        2. Numbers differ (sorted) → NOT duplicate
        3. similarity < 0.98 AND new more detailed → NOT duplicate
        4. Otherwise → DUPLICATE
        
        Args:
            fact: Fact entity to add
            similarity_threshold: Minimum similarity for duplicate check (default: 0.96)
            
        Returns:
            Tuple (was_added: bool, fact_id_or_duplicate_id: str)
        """
        if not fact.vector:
            fact_id = await self.add_fact(fact)
            return True, fact_id

        # Convert similarity threshold to distance threshold
        # similarity > 0.96  =>  distance < 0.04
        distance_threshold = 1.0 - similarity_threshold

        # Search for the nearest neighbor
        vector_query = self.facts_col.where(
            filter=FieldFilter("account_id", "==", fact.account_id)
        ).where(
            filter=FieldFilter("is_current", "==", True)
        ).find_nearest(
            vector_field="vector",
            query_vector=fact.vector,  # REMOVED Vector() wrapper
            distance_measure=DistanceMeasure.COSINE,
            limit=1,
            distance_threshold=distance_threshold
        )

        docs = await vector_query.get()
        
        if docs:
            # Found similar fact → run smart deduplication
            existing_doc = docs[0]
            existing_data = existing_doc.to_dict()
            
            # Convert Vector back to list for similarity calculation
            if 'vector' in existing_data and isinstance(existing_data['vector'], Vector):
                existing_data['vector'] = list(existing_data['vector'])
            
            existing_data = self._migrate_ownership_fields(existing_data)
            existing_fact = FactEntity(**existing_data)
            
            # Calculate exact similarity using domain utility
            similarity = cosine_similarity(fact.vector, existing_fact.vector)
            
            # Use SmartDeduplicationService for intelligent comparison
            is_duplicate, reason = self._dedup_service.is_duplicate(
                fact.text,
                existing_fact.text,
                similarity
            )
            
            if is_duplicate:
                logger.debug(
                    f"⏭️  [Dedup] Duplicate detected: {reason} | "
                    f"similarity={similarity:.3f} | "
                    f"new='{fact.text[:50]}...' | "
                    f"existing_id={existing_doc.id[:8]}"
                )
                return False, existing_doc.id
            else:
                logger.debug(
                    f"✅ [Dedup] NOT duplicate: {reason} | "
                    f"similarity={similarity:.3f} | "
                    f"adding new fact: '{fact.text[:50]}...'"
                )

        # No duplicate found (or smart dedup said NOT duplicate), add the fact
        fact_id = await self.add_fact(fact)
        return True, fact_id

    def _check_production_safety(self, operation: str) -> None:
        """
        Check if dangerous operations are allowed in production environment.

        Args:
            operation: Name of the dangerous operation

        Raises:
            PermissionError: If operation is blocked in production
        """
        if self.env_config.is_production:
            dangerous_ops = [
                "archive_observations",  # This deletes observations
                "delete_all_facts",      # Would be added if we had this method
                "rebuild_memory",        # Would be added if we had this method
            ]

            if operation in dangerous_ops:
                raise PermissionError(
                    f"❌ Operation '{operation}' blocked in PRODUCTION environment. "
                    "This operation could delete or modify production data. "
                    "Use explicit override if absolutely necessary."
                )

    async def get_biographical_context(
        self,
        owner_id: str,
        limit: int = 100
    ) -> List[FactEntity]:
        """
        Legacy method for backward compatibility.
        Delegates to cached version.

        Args:
            owner_id: Account ID (kept as owner_id for backward compatibility)
        """
        cached_dicts = await self.get_biographical_context_cached(owner_id, limit)
        return [FactEntity(
            account_id=owner_id,
            created_by_user_id=owner_id,  # Assume same for biographical facts
            lineage_id="", # Metadata only
            text=d["text"],
            tags=d["tags"],
            type=FactType(d["type"]),
            is_current=True
        ) for d in cached_dicts]

    async def refresh_biographical_context_cache(
        self, 
        owner_id: str,
        facts_limit: Optional[int] = None,
        principles_limit: Optional[int] = None
    ) -> None:
        """
        Refresh cached biographical context using injected BiographicalContextService.

        SESSION 2026-02-07: Hexagonal Architecture Refactoring
        Repository (Infrastructure) DELEGATES to Application Service (DI).
        No longer creates services internally - respects dependency direction.

        Fire-and-forget safe: errors are logged but don't stop the flow.

        Session: docs/SESSION_2026_02_07_BIOGRAPHICAL_CACHE_OPTIMIZATION.md
        RFC: docs/10_rfcs/BIOGRAPHICAL_CACHE_MULTI_VECTOR_RFC.md
        
        Args:
            owner_id: Account ID
            facts_limit: Max biographical facts (None = use system default from service)
            principles_limit: Max principles (None = use system default from service)
        """
        # SESSION 2026-02-07: Check if service is injected
        if not self._biographical_context_service:
            logger.warning(
                f"⚠️ [FirestoreRepo] BiographicalContextService not injected, "
                f"skipping cache refresh for {owner_id[:8]}"
            )
            return

        try:
            logger.info(f"🔄 [FirestoreRepo] Refreshing biographical cache for {owner_id[:8]}...")

            # DELEGATE to injected Application Service
            # Service already has configured limits from its constructor
            # Note: facts_limit/principles_limit parameters ignored - service uses its own config
            context = await self._biographical_context_service.refresh_context(owner_id)

            # Save to cache with version marker (Infrastructure responsibility)
            cache_doc = {
                "biographical_facts": context["facts"],
                "principles": context["principles"],
                "refreshed_at": firestore.SERVER_TIMESTAMP,
                "facts_count": len(context["facts"]),
                "principles_count": len(context["principles"]),
                "version": "v3_hexagonal_di"  # New version marker
            }

            await self.user_context_col.document(owner_id).set(cache_doc, merge=True)

            logger.info(
                f"✅ [FirestoreRepo] Cached {len(context['facts'])} facts + "
                f"{len(context['principles'])} principles for {owner_id[:8]}"
            )

        except Exception as e:
            logger.error(
                f"❌ [FirestoreRepo] Failed to refresh biographical cache for {owner_id}: {e}",
                exc_info=True
            )

    async def get_biographical_context_cached(
        self,
        owner_id: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict]:
        """
        Retrieves biographical context from cache (fast read).
        
        SESSION_27: Auto-resolves owner_id from RequestContext if not provided.
        SESSION 2026-02-08: Simplified - no backward compatibility, v3+ only.

        Args:
            owner_id: Optional account ID. If None, auto-resolved from RequestContext.
            limit: Max facts to return (default: 100)

        Returns:
            List of biographical fact dictionaries (facts + principles merged)
        """
        # Auto-resolve owner_id from RequestContext if not provided
        if owner_id is None:
            from ..domain.request_context import get_effective_account_id
            owner_id = get_effective_account_id()
            logger.debug(f"🔍 [Cache] Auto-resolved owner_id from RequestContext: {owner_id[:12]}...")
        
        if not owner_id:
            logger.warning("⚠️ [Cache] No owner_id available")
            return []

        try:
            doc = await self.user_context_col.document(owner_id).get()

            if not doc.exists:
                logger.warning(f"⚠️ [Cache] No cache found for {owner_id[:12]}...")
                return []

            data = doc.to_dict()
            
            # Load facts and principles (v3+ cache structure)
            facts = data.get("biographical_facts", [])
            principles = data.get("principles", [])
            
            # Merge both lists (formatter will separate by type)
            combined = facts + principles
            
            if not combined:
                logger.warning(f"⚠️ [Cache] Empty cache for {owner_id[:12]}...")
                return []
            
            # Sanitize dates (Firestore timestamps → ISO strings)
            sanitized = []
            for item in combined:
                if not isinstance(item, dict):
                    continue
                
                fact = dict(item)
                created_at = fact.get("created_at")
                if hasattr(created_at, "isoformat"):
                    fact["created_at"] = created_at.isoformat()
                
                sanitized.append(fact)
            
            logger.debug(
                f"📖 [Cache] Loaded {len(facts)} facts + {len(principles)} principles "
                f"for {owner_id[:12]}... (total: {len(sanitized)})"
            )

            # SESSION 2026-02-08: Return FULL cache (limits applied during cache refresh)
            return sanitized

        except Exception as e:
            logger.error(f"❌ [Cache] Error reading biographical context: {e}", exc_info=True)
            return []

    @log_execution_time
    async def search_facts_by_domain(
        self,
        domains: List[str],
        limit: int = 10,
        account_id: Optional[str] = None,
        user_id: Optional[str] = None
    ) -> List[FactEntity]:
        """
        Direct Firestore query by domain (uses existing index).
        
        Index: (account_id, domain, created_at, __name__)
        Query: WHERE account_id=X AND domain IN [Y,Z] AND state="current"
        
        Session 2026-02-17: Domain-based search for router enrichment.
        
        Args:
            domains: List of domain values (e.g., ["health", "possession"])
            limit: Maximum number of facts to return
            account_id: Explicit account ID override (optional)
            user_id: Explicit user ID override (optional)
        
        Returns:
            List of facts from specified domains
        """
        from ..domain.request_context import get_current_account_id
        
        # Resolve account_id (explicit or from context)
        resolved_account_id = account_id or get_current_account_id()
        
        if not resolved_account_id:
            raise ValueError("account_id required for domain search")
        
        if not domains:
            logger.debug("🔍 [Domain Search] No domains specified, returning empty")
            return []
        
        # Firestore supports up to 30 values in IN operator
        # Router will send max 1-3 domains so we're safe
        query = self.facts_col.where(
            filter=FieldFilter("account_id", "==", resolved_account_id)
        ).where(
            filter=FieldFilter("domain", "in", domains)
        ).where(
            filter=FieldFilter("state", "==", "current")
        ).order_by("created_at").limit(limit)
        
        docs = await query.get()
        
        results = []
        for doc in docs:
            data = doc.to_dict()
            # Convert vectors
            if 'vector' in data and isinstance(data['vector'], Vector):
                data['vector'] = list(data['vector'])
            if 'tags_vector' in data and isinstance(data['tags_vector'], Vector):
                data['tags_vector'] = list(data['tags_vector'])
            if 'metadata_vector' in data and isinstance(data['metadata_vector'], Vector):
                data['metadata_vector'] = list(data['metadata_vector'])
            data = self._migrate_ownership_fields(data)
            results.append(FactEntity(**data))
        
        logger.info(
            f"🔍 [Domain Search] Found {len(results)} facts in domains {domains} "
            f"for account {resolved_account_id[:8]}..."
        )
        return results

    async def get_legacy_facts(
        self,
        account_id: str,
        limit: int = 20
    ) -> List[FactEntity]:
        """
        Retrieves legacy facts (missing domain taxonomy) for migration.
        
        Session: 2026-02-17 Legacy Fact Migration
        Query: account_id = X AND domain IS NULL (filter superseded in Python)
        Order: created_at ASC (oldest first for chronological processing)
        
        Note: Firestore != operator doesn't return docs where field is missing,
        so we filter superseded facts in Python after query.
        
        Args:
            account_id: Account ID to migrate
            limit: Maximum number of facts to return (default: 20)
            
        Returns:
            List of legacy facts ordered by created_at ASC
        """
        try:
            # Firestore where("domain", "==", None) doesn't find docs where field is MISSING
            # Solution: Stream all account facts and filter + sort in Python
            # NO order_by to avoid requiring another index
            query = self.facts_col.where(filter=FieldFilter("account_id", "==", account_id))
            
            results = []
            
            # Stream and filter in Python
            async for doc in query.stream():
                data = doc.to_dict()
                
                # Filter: domain must be missing or None
                if "domain" in data and data["domain"] is not None:
                    continue
                
                # Filter out superseded facts
                fact_state = data.get("state", "MISSING")
                if fact_state == "superseded":
                    continue
                
                # Convert Vector back to list if needed
                if 'vector' in data and isinstance(data['vector'], Vector):
                    data['vector'] = list(data['vector'])
                if 'tags_vector' in data and isinstance(data['tags_vector'], Vector):
                    data['tags_vector'] = list(data['tags_vector'])
                if 'metadata_vector' in data and isinstance(data['metadata_vector'], Vector):
                    data['metadata_vector'] = list(data['metadata_vector'])
                
                # Migrate ownership fields for backward compatibility
                data = self._migrate_ownership_fields(data)
                results.append(FactEntity(**data))
                
                # Stop once we have enough facts
                if len(results) >= limit:
                    break
            
            logger.info(
                f"📦 [get_legacy_facts] Found {len(results)} legacy facts "
                f"for account {account_id[:8]}..."
            )
            
            return results
            
        except Exception as e:
            logger.error(
                f"❌ [get_legacy_facts] Error querying legacy facts: {e}",
                exc_info=True
            )
            return []

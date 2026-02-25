"""
Fact Write Service
==================

Service for writing facts with automatic multi-vector generation and deduplication.

Session: 2026-02-07 Hexagonal Architecture Refactoring
Purpose: Extract embedding generation from ConsolidationAgent to maintain clean separation.

Architecture:
- Application Layer Service
- Depends on Ports: FactRepository, EmbeddingService
- Used by: ConsolidationAgent (Domain)
- Analogue: SearchEnrichmentService (for reading)

Responsibilities:
- Generate multi-vector embeddings (Infrastructure work)
- Semantic deduplication (Domain logic via Repository)
- Batch processing for efficiency
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple
from ..domain.entities import (
    FactEntity,
    FactType,
    FactDomain,
    TemporalClass,
    FactState,
    ContextPriority,
)
from ..ports.repository import FactRepository
from ..ports.embedding_service import EmbeddingService
from ..ports.fact_write_port import FactWritePort
from ..utils.logger import logger


class FactWriteService(FactWritePort):
    """
    Service for writing facts with automatic multi-vector embedding generation.
    
    This service extracts Infrastructure concerns (embedding generation) from Domain agents,
    maintaining hexagonal architecture principles.
    
    Pattern:
    - Agent (Domain): Synthesizes data from LLM → Provides facts_data (no vectors)
    - Service (Application): Generates embeddings → Creates FactEntity with vectors
    - Repository (Infrastructure): Saves to database with deduplication
    
    Similar to SearchEnrichmentService but for writes instead of reads.
    """

    def __init__(
        self,
        repository: FactRepository,
        embedding_service: EmbeddingService
    ):
        """
        Initialize fact write service with dependencies.
        
        Args:
            repository: Fact repository for storage (Port)
            embedding_service: Embedding service for vector generation (Port)
        """
        self._repo = repository
        self._embedding = embedding_service
        
        logger.info("📝 [FactWriteService] Initialized")

    async def add_facts_batch(
        self,
        account_id: str,
        user_id: str,
        facts_data: List[Dict],
        skip_deduplication: bool = False
    ) -> Tuple[int, int]:
        """
        Add batch of facts with automatic multi-vector generation and deduplication.
        
        Process:
        1. Generate 3 embeddings per fact (text, tags, metadata) in parallel
        2. Create FactEntity with all vectors
        3. Save with semantic deduplication (Repository handles similarity check)
        
        Args:
            account_id: Account ID (billing entity)
            user_id: User ID (attribution)
            facts_data: List of fact dicts from LLM with keys:
                - text/content: Fact text
                - tags: List of tags
                - type: Fact type (event, state, principle, etc.)
                - metadata: Optional metadata dict
            skip_deduplication: If True, skip semantic deduplication (Deliberate Fact Management)
        
        Returns:
            Tuple (saved_count, skipped_count)
        
        Example:
            facts_data = [
                {"text": "User owns 2005 Honda Civic", "tags": ["vehicle", "car"], "type": "event"},
                {"text": "User dislikes Brussels sprouts", "tags": ["food", "preference"], "type": "state"}
            ]
            saved, skipped = await service.add_facts_batch(account_id, user_id, facts_data)
        """
        if not facts_data:
            return 0, 0, []
        
        logger.info(f"📝 [FactWriteService] Adding {len(facts_data)} facts for account {account_id[:8]}...")
        
        # Generate multi-vectors for all facts in parallel
        multi_vectors_tasks = [
            self._generate_multi_vectors(fact_data) for fact_data in facts_data
        ]
        all_vectors = await asyncio.gather(*multi_vectors_tasks)
        
        # Save facts one by one (semantic deduplication requires sequential processing)
        saved_count = 0
        skipped_count = 0
        saved_ids: List[str] = []

        for fact_data, vectors in zip(facts_data, all_vectors):
            # Extract data
            text = fact_data.get("content") or fact_data.get("text")
            tags = fact_data.get("tags", [])
            
            # Map LLM type to FactType enum
            llm_type = fact_data.get("type", "event").lower()
            if llm_type == "state":
                fact_type = FactType.STATE
            elif llm_type == "principle":
                fact_type = FactType.PRINCIPLE
            elif llm_type == "system":
                fact_type = FactType.SYSTEM
            elif llm_type == "alert":
                fact_type = FactType.ALERT
            else:
                fact_type = FactType.EVENT  # Default
            
            # Add "consolidated" tag
            if "consolidated" not in tags:
                tags.append("consolidated")
            
            # Add "anchor" tag for principles
            if fact_type == FactType.PRINCIPLE and "anchor" not in tags:
                tags.append("anchor")

            # Resolve Deliberate Fact Management taxonomy fields (optional)
            domain_value = fact_data.get("domain")
            temporal_value = fact_data.get("temporal_class")
            state_value = fact_data.get("state")
            context_priority_value = fact_data.get("context_priority")

            domain = FactDomain(domain_value) if domain_value else None
            temporal_class = TemporalClass(temporal_value) if temporal_value else None
            state = FactState(state_value) if state_value else FactState.CURRENT
            context_priority = (
                ContextPriority(context_priority_value)
                if context_priority_value
                else ContextPriority.MEDIUM
            )
            
            # Create FactEntity with multi-vectors
            fact_entity = FactEntity(
                account_id=account_id,
                created_by_user_id=user_id,
                lineage_id=str(uuid.uuid4()),  # Generate unique lineage ID
                text=text,
                tags=tags,
                type=fact_type,
                metadata=fact_data.get("metadata", {}),
                is_current=True,
                domain=domain,
                temporal_class=temporal_class,
                state=state,
                context_priority=context_priority,
                ttl_days=fact_data.get("ttl_days"),
                context=fact_data.get("context"),
                reported_date=fact_data.get("reported_date") or datetime.now(timezone.utc),
                # Multi-vector embeddings
                vector=vectors["vector"],
                tags_vector=vectors["tags_vector"],
                metadata_vector=vectors["metadata_vector"]
            )
            
            if skip_deduplication:
                fact_id = await self._repo.add_fact(fact_entity)
                saved_ids.append(fact_id)
                was_added = True
                duplicate_id = None
            else:
                # Save with semantic deduplication
                # SESSION 2026-02-08: Raised threshold from 0.85 to 0.96
                was_added, duplicate_id = await self._repo.add_fact_if_unique(
                    fact_entity,
                    similarity_threshold=0.96
                )
            
            if was_added:
                saved_count += 1
            else:
                skipped_count += 1
                logger.debug(
                    f"⏭️  Skipped semantic duplicate (existing: {duplicate_id[:8]}...): '{text[:50]}...'"
                )
        
        if skip_deduplication:
            logger.info(
                f"✅ [FactWriteService] Completed: {saved_count} saved (deduplication skipped)"
            )
        else:
            logger.info(
                f"✅ [FactWriteService] Completed: {saved_count} saved, {skipped_count} duplicates"
            )

        return saved_count, skipped_count, saved_ids

    async def _generate_multi_vectors(self, fact_data: Dict) -> Dict[str, List[float]]:
        """
        Generate 3 vectors for multi-vector search (Session 2026-02-07).
        
        Generates:
        1. vector: Semantic embedding of fact text (RETRIEVAL_DOCUMENT task)
        2. tags_vector: Domain keywords embedding (SEMANTIC_SIMILARITY task)
        3. metadata_vector: Structured data embedding (SEMANTIC_SIMILARITY task)
        
        Args:
            fact_data: Fact dict from LLM with 'content'/'text', 'tags', 'metadata'
            
        Returns:
            Dict with keys: vector, tags_vector, metadata_vector (each 768 dims)
            
        Example:
            fact_data = {
                "content": "User's mother is Valentina",
                "tags": ["family", "mother"],
                "metadata": {"relationship": "mother", "name": "Valentina"}
            }
            
            vectors = await self._generate_multi_vectors(fact_data)
            # → {"vector": [0.1, ...], "tags_vector": [0.2, ...], "metadata_vector": [0.3, ...]}
        """
        text = fact_data.get("content") or fact_data.get("text", "")
        tags = fact_data.get("tags", [])
        metadata = fact_data.get("metadata", {})
        
        # Prepare embedding texts
        tags_text = " ".join(tags) if tags else text  # Fallback to text if no tags
        metadata_text = json.dumps(metadata, ensure_ascii=False) if metadata else text
        
        # Single batch call: 3 texts → 3 vectors in one HTTP request (~5s vs ~15s).
        # genai.Client serializes concurrent to_thread calls at the HTTP layer,
        # so asyncio.gather with 3 get_embedding() calls was effectively sequential.
        # All three use "RETRIEVAL_DOCUMENT" — consistent with the "RETRIEVAL_QUERY"
        # vectors used at search time (SearchEnrichmentService). Previously
        # tags/metadata used "SEMANTIC_SIMILARITY" which was already mismatched
        # with the "RETRIEVAL_QUERY" search vectors.
        vector, tags_vector, metadata_vector = await self._embedding.get_embeddings_batch(
            [text, tags_text, metadata_text], "RETRIEVAL_DOCUMENT"
        )
        
        logger.debug(
            f"   ✓ Generated multi-vectors for: '{text[:30]}...' "
            f"(tags={len(tags)}, metadata_keys={len(metadata)})"
        )
        
        return {
            "vector": vector,
            "tags_vector": tags_vector,
            "metadata_vector": metadata_vector
        }

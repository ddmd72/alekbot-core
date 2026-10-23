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
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Sequence, Tuple
from ..domain.fact_embedding import fact_embedding_texts
from ..domain.entities import FactEntity, FactType
from ..domain.new_fact_attributes import NewFactAttributes, parse_new_fact
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
    ) -> Tuple[int, int, List[str]]:
        """
        Add batch of facts with automatic multi-vector generation and deduplication.

        Process:
        1. Parse every fact (a bad value raises ValueError before any embedding or write)
        2. build_facts: 3 embeddings per fact, FactEntity with all vectors
        3. Save one by one (skip_deduplication) or with semantic deduplication

        Args:
            account_id: Account ID (billing entity)
            user_id: User ID (attribution)
            facts_data: fact dicts with text/content, tags, type, metadata and the optional taxonomy fields
            skip_deduplication: If True, skip semantic deduplication (Deliberate Fact Management)

        Returns:
            Tuple (saved_count, skipped_count, saved_ids)
        """
        if not facts_data:
            return 0, 0, []

        parsed = [parse_new_fact(fact_data) for fact_data in facts_data]

        logger.info(f"📝 [FactWriteService] Adding {len(parsed)} facts for account {account_id[:8]}...")

        entities = await self.build_facts(account_id, user_id, parsed)

        # Semantic deduplication requires sequential processing
        saved_count = 0
        skipped_count = 0
        saved_ids: List[str] = []

        for fact_entity in entities:
            if skip_deduplication:
                fact_id = await self._repo.add_fact(fact_entity)
                saved_ids.append(fact_id)
                was_added = True
                duplicate_id = None
            else:
                # Raised from 0.85 to 0.96 on 2026-02-08
                was_added, duplicate_id = await self._repo.add_fact_if_unique(
                    fact_entity,
                    similarity_threshold=0.96
                )

            if was_added:
                saved_count += 1
            else:
                skipped_count += 1
                logger.debug(f"⏭️  Skipped semantic duplicate (existing: {(duplicate_id or '')[:8]}...)")

        if skip_deduplication:
            logger.info(
                f"✅ [FactWriteService] Completed: {saved_count} saved (deduplication skipped)"
            )
        else:
            logger.info(
                f"✅ [FactWriteService] Completed: {saved_count} saved, {skipped_count} duplicates"
            )

        return saved_count, skipped_count, saved_ids

    async def build_facts(
        self,
        account_id: str,
        user_id: str,
        facts: Sequence[NewFactAttributes],
    ) -> List[FactEntity]:
        """Embed and build entities from parsed attributes; nothing is stored. Tags are appended in place."""
        if not facts:
            return []

        all_vectors = await asyncio.gather(*(self._generate_multi_vectors(attrs) for attrs in facts))

        entities: List[FactEntity] = []
        for attrs, vectors in zip(facts, all_vectors):
            if len(vectors) != 3 or any(vector is None for vector in vectors):
                logger.error(f"❌ [FactWriteService] Embedding service returned no vector (type={attrs.type.value})")
                raise RuntimeError("The embedding service returned no vector for a fact")

            tags = attrs.tags
            if "consolidated" not in tags:
                tags.append("consolidated")
            if attrs.type == FactType.PRINCIPLE and "anchor" not in tags:
                tags.append("anchor")

            entities.append(FactEntity(
                account_id=account_id,
                created_by_user_id=user_id,
                lineage_id=str(uuid.uuid4()),
                text=attrs.content,
                tags=tags,
                type=attrs.type,
                metadata=attrs.metadata,
                is_current=True,
                domain=attrs.domain,
                temporal_class=attrs.temporal_class,
                state=attrs.state,
                context_priority=attrs.context_priority,
                ttl_days=attrs.ttl_days,
                context=attrs.context,
                reported_date=attrs.reported_date or datetime.now(timezone.utc),
                vector=vectors[0],
                tags_vector=vectors[1],
                metadata_vector=vectors[2],
            ))
        return entities

    async def _generate_multi_vectors(self, attrs: NewFactAttributes) -> List[List[float]]:
        """The three vectors of a fact (text, tags, metadata), one batch call, task RETRIEVAL_DOCUMENT as at search time."""
        texts = fact_embedding_texts(attrs.content, attrs.tags, attrs.metadata)
        # One batch call: genai.Client serializes concurrent to_thread calls, so three single calls were sequential.
        vectors = await self._embedding.get_embeddings_batch(texts, "RETRIEVAL_DOCUMENT")
        logger.debug(f"   ✓ Generated multi-vectors (tags={len(attrs.tags)}, metadata_keys={len(attrs.metadata)})")
        return list(vectors)

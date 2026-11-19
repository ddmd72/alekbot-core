"""
Fact Write Service
==================

Builds new facts with their multi-vector embeddings; the caller stores them.

Architecture:
- Application Layer Service
- Depends on Ports: EmbeddingService
- Used by: FactManagementAdapter (create, merge, the update's history copy)
- Analogue: SearchEnrichmentService (for reading)
"""

import asyncio
import uuid
from datetime import datetime, timezone
from typing import List, Sequence
from ..domain.fact_embedding import fact_embedding_texts
from ..domain.entities import FactEntity, FactType
from ..domain.new_fact_attributes import NewFactAttributes
from ..ports.embedding_service import EmbeddingService
from ..ports.fact_write_port import FactWritePort
from ..utils.logger import logger


class FactWriteService(FactWritePort):
    """Builds new FactEntity objects with their three vectors; the caller (FactManagementAdapter) stores them."""

    def __init__(self, embedding_service: EmbeddingService):
        self._embedding = embedding_service
        
        logger.info("📝 [FactWriteService] Initialized")

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
                directive=attrs.directive,
                temporal_class=attrs.temporal_class,
                state=attrs.state,
                context_priority=attrs.context_priority,
                context=attrs.context,
                reported_date=attrs.reported_date or datetime.now(timezone.utc),
                vector=vectors[0],
                tags_vector=vectors[1],
                metadata_vector=vectors[2],
                **(attrs.expiry_fields or {}),
            ))
        return entities

    async def _generate_multi_vectors(self, attrs: NewFactAttributes) -> List[List[float]]:
        """The three vectors of a fact (text, tags, metadata), one batch call, task RETRIEVAL_DOCUMENT as at search time."""
        texts = fact_embedding_texts(attrs.content, attrs.tags, attrs.metadata)
        # One batch call: genai.Client serializes concurrent to_thread calls, so three single calls were sequential.
        vectors = await self._embedding.get_embeddings_batch(texts, "RETRIEVAL_DOCUMENT")
        logger.debug(f"   ✓ Generated multi-vectors (tags={len(attrs.tags)}, metadata_keys={len(attrs.metadata)})")
        return list(vectors)

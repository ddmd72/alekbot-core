import uuid
import asyncio
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from ..domain.entities import (
    FactEntity,
    FactDomain,
    TemporalClass,
    FactState,
    ContextPriority,
    normalize_fact_taxonomy,
)
from ..domain.search import SearchLimits
from ..ports.fact_management_port import FactManagementPort
from ..ports.repository import FactRepository
from ..ports.embedding_service import EmbeddingService
from ..ports.fact_write_port import FactWritePort
from ..ports.search_enrichment_port import SearchEnrichmentPort
from ..utils.logger import logger


class FactManagementAdapter(FactManagementPort):
    """
    Implementation of deliberate fact management tools.

    Coordinates FactRepository, EmbeddingService, FactWriteService and
    SearchEnrichmentService to provide search/create/update/merge/discard
    operations for ConsolidationAgent v3.

    Session 2026-02-16: Deliberate Fact Management RFC
    Session 2026-02-20: Renamed from FirestoreFactManagementAdapter (does not
    access Firestore directly — orchestrates ports only).
    """

    def __init__(
        self,
        repository: FactRepository,
        embedding_service: EmbeddingService,
        fact_write_service: FactWritePort,
        search_enrichment_service: SearchEnrichmentPort,
        max_retries: int = 3
    ) -> None:
        self._repo = repository
        self._embedding = embedding_service
        self._fact_write_service = fact_write_service
        self._search_enrichment = search_enrichment_service
        self._max_retries = max_retries
        
        logger.info(
            "🔧 [FactManagement] Initialized with multi-vector search support "
            "(SearchEnrichmentService)"
        )

    async def search_existing_facts(
        self,
        keywords: List[str],
        primary_query: str,
        alternative_query: str = "",
        limit: int = 20
    ) -> List[Dict[str, Any]]:
        """
        Search existing facts using multi-vector RRF strategy.
        
        Session 2026-02-16: Updated to use SearchEnrichmentService
        - Multi-vector search (text + metadata + tags)
        - RRF ranking for quality results
        - skip_semantic_dedup=True (keep ALL facts with different IDs)
        - NO domain filter (consolidation needs all relevant facts)
        
        Args:
            keywords: Domain keywords for tag-based search
            primary_query: Main semantic search phrase
            alternative_query: Alternative phrasing (optional)
            limit: Max results to return (default: 20)
            
        Returns:
            List of fact dictionaries with fact_id, content, similarity, source
        """
        try:
            logger.info(
                f"🔍 [FactManagement] Multi-vector search: "
                f"keywords={keywords[:3]}{'...' if len(keywords) > 3 else ''}, "
                f"primary='{primary_query[:40]}...', limit={limit}"
            )
            
            # Call SearchEnrichmentService with consolidation mode
            enriched = await self._search_enrichment.enrich_context(
                keywords=keywords,
                search_phrase_1=primary_query,
                search_phrase_2=alternative_query or primary_query,
                limits=SearchLimits(
                    keyword_limit=7,
                    phrase_one_limit=10,
                    phrase_two_limit=10,
                    total_limit=limit,
                ),
                skip_semantic_dedup=True,  # Keep ALL facts with different IDs for MERGE!
            )

            # Convert EnrichedFact → Dict — taxonomy fields already carried by EnrichedFact
            results = []
            for ef in enriched.facts:
                results.append({
                    "fact_id": ef.fact_id,
                    "content": ef.content,
                    "similarity": ef.relevance_score,
                    "source": ef.source,
                    "type": ef.fact_type,
                    "domain": ef.domain,
                    "temporal_class": ef.temporal_class,
                    "state": ef.state,
                    "context_priority": ef.context_priority,
                    "tags": ef.tags or [],
                    "metadata": ef.metadata or {},
                    "reported_date": ef.reported_date,
                    "version": ef.version,
                })

            # Log top 3 results (with safe similarity formatting)
            for i, result in enumerate(results[:3]):
                sim = result.get('similarity')
                sim_str = f"{sim:.3f}" if sim is not None else "N/A"
                logger.info(
                    f"   [{i+1}] fact_id={result['fact_id'][:8]}... "
                    f"similarity={sim_str} "
                    f"source={result['source']} "
                    f"domain={result['domain']} "
                    f"content='{result['content'][:60]}...'"
                )

            logger.info(
                f"✅ [FactManagement] Found {len(results)} facts "
                f"(multi-vector RRF, semantic_dedup=SKIPPED, id_dedup_only=True)"
            )

            return results
            
        except Exception as e:
            logger.error(f"❌ [FactManagement] Search failed: {e}", exc_info=True)
            return []  # Graceful degradation

    async def create_fact(self, content: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create new fact with validation and error handling.
        
        Args:
            content: Fact text
            metadata: Fact metadata (must include account_id, user_id, domain, etc.)
            
        Returns:
            Result dict with fact_id, status, message
        """
        try:
            # 🔍 DEBUG: Log full tool call for analysis
            logger.info(
                f"🔧 [FactManagement] create_fact() TOOL CALL:\n"
                f"   content='{content[:80]}...'\n"
                f"   metadata keys={list(metadata.keys())}\n"
                f"   metadata={metadata}"
            )
            
            # Validate required fields
            required = ["account_id", "user_id", "domain", "temporal_class", "context_priority"]
            missing = [f for f in required if f not in metadata]
            if missing:
                logger.error(f"❌ [FactManagement] Missing required fields: {missing}")
                return {
                    "fact_id": None,
                    "status": "failed",
                    "message": f"Missing required fields: {missing}"
                }
            
            # Domain-level normalization (was duplicated here — now in domain/entities.py)
            metadata = normalize_fact_taxonomy(metadata)

            fact_data = {
                "content": content,
                "tags": metadata.get("tags", []),
                "type": metadata.get("type", "event"),
                "metadata": metadata.get("metadata", {}),
                "domain": metadata.get("domain"),
                "temporal_class": metadata.get("temporal_class"),
                "state": metadata.get("state"),
                "context_priority": metadata.get("context_priority"),
                "ttl_days": metadata.get("ttl_days"),
                "context": metadata.get("context"),
                "reported_date": metadata.get("reported_date") or datetime.now(timezone.utc).isoformat(),
            }

            saved, _, saved_ids = await self._fact_write_service.add_facts_batch(
                account_id=metadata["account_id"],
                user_id=metadata["user_id"],
                facts_data=[fact_data],
                skip_deduplication=True
            )

            if saved:
                logger.info(f"✅ [FactManagement] Created fact: '{content[:50]}...'")
            else:
                logger.warning(f"⚠️  [FactManagement] Failed to create fact: '{content[:50]}...'")

            return {
                "fact_id": saved_ids[0] if saved_ids else None,
                "status": "created" if saved else "failed",
                "message": "Fact created successfully" if saved else "Fact creation failed",
            }
            
        except Exception as e:
            logger.error(f"❌ [FactManagement] Create fact error: {e}", exc_info=True)
            return {
                "fact_id": None,
                "status": "failed",
                "message": f"Error creating fact: {str(e)}"
            }

    async def update_fact(self, fact_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        """
        Update existing fact with error handling.
        
        Args:
            fact_id: UUID of fact to update
            updates: Fields to update (content, tags, state, etc.)
            
        Returns:
            Result dict with status, version, message
        """
        try:
            logger.debug(f"🔄 [FactManagement] Updating fact {fact_id[:8]}...")
            
            existing = await self._repo.get_fact_by_id(fact_id)
            if not existing:
                logger.warning(f"⚠️  [FactManagement] Fact not found: {fact_id[:8]}")
                return {
                    "fact_id": fact_id,
                    "status": "not_found",
                    "message": "Fact not found",
                }

            new_content = updates.get("content", existing.text)
            new_tags = updates.get("tags", existing.tags)
            new_state = updates.get("state")
            new_temporal_class = updates.get("temporal_class")

            # Normalize to lowercase (LLM returns UPPERCASE, enums expect lowercase)
            if new_state:
                new_state = new_state.lower()
                existing.state = FactState(new_state)
            
            if new_temporal_class:
                new_temporal_class = new_temporal_class.lower()
                existing.temporal_class = TemporalClass(new_temporal_class)

            existing.text = new_content
            existing.tags = new_tags
            existing.version = (existing.version or 1) + 1
            existing.last_updated = datetime.now(timezone.utc)
            reported_raw = updates.get("reported_date")
            if isinstance(reported_raw, str):
                existing.reported_date = datetime.fromisoformat(reported_raw.replace("Z", "+00:00"))
            elif isinstance(reported_raw, datetime):
                existing.reported_date = reported_raw
            else:
                existing.reported_date = datetime.now(timezone.utc)

            await self._repo.update_fact(existing)

            logger.info(
                f"✅ [FactManagement] Updated fact {fact_id[:8]} to version {existing.version}"
            )

            return {
                "fact_id": fact_id,
                "status": "updated",
                "version": existing.version,
                "message": "Fact updated successfully",
            }
            
        except Exception as e:
            logger.error(f"❌ [FactManagement] Update fact error: {e}", exc_info=True)
            return {
                "fact_id": fact_id,
                "status": "failed",
                "message": f"Error updating fact: {str(e)}"
            }

    async def merge_facts(
        self,
        fact_ids: List[str],
        merged_content: str,
        metadata: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Merge multiple facts into one with transaction-like behavior.
        
        Args:
            fact_ids: List of fact UUIDs to merge
            merged_content: Combined fact text
            metadata: Metadata for new merged fact
            
        Returns:
            Result dict with new_fact_id, old_fact_ids, status, message
        """
        try:
            logger.info(f"🔀 [FactManagement] Merging {len(fact_ids)} facts...")
            
            superseded_ids = []
            for fact_id in fact_ids:
                try:
                    existing = await self._repo.get_fact_by_id(fact_id)
                    if not existing:
                        logger.warning(f"⚠️  [FactManagement] Fact not found for merge: {fact_id[:8]}")
                        continue
                    existing.state = FactState.SUPERSEDED
                    existing.is_current = False
                    existing.valid_to = datetime.now(timezone.utc)
                    await self._repo.update_fact(existing)
                    superseded_ids.append(fact_id)
                except Exception as e:
                    logger.warning(f"⚠️  [FactManagement] Failed to supersede fact {fact_id[:8]}: {e}")

            if not superseded_ids:
                logger.error("❌ [FactManagement] No facts successfully superseded")
                return {
                    "new_fact_id": None,
                    "old_fact_ids": [],
                    "old_facts_state": "FAILED",
                    "status": "failed",
                    "message": "Failed to supersede any facts"
                }

            # Domain-level normalization (was duplicated here — now in domain/entities.py)
            metadata = normalize_fact_taxonomy(metadata)

            fact_data = {
                "content": merged_content,
                "tags": metadata.get("tags", []),
                "type": metadata.get("type", "event"),
                "metadata": metadata.get("metadata", {}),
                "domain": metadata.get("domain"),
                "temporal_class": metadata.get("temporal_class"),
                "state": metadata.get("state"),
                "context_priority": metadata.get("context_priority"),
                "ttl_days": metadata.get("ttl_days"),
                "context": metadata.get("context"),
                "reported_date": metadata.get("reported_date") or datetime.now(timezone.utc).isoformat(),
            }

            saved, _, saved_ids = await self._fact_write_service.add_facts_batch(
                account_id=metadata["account_id"],
                user_id=metadata["user_id"],
                facts_data=[fact_data],
                skip_deduplication=True
            )

            if saved:
                logger.info(
                    f"✅ [FactManagement] Merged {len(superseded_ids)} facts into new fact"
                )
            else:
                logger.error("❌ [FactManagement] Failed to create merged fact")

            return {
                "new_fact_id": saved_ids[0] if saved_ids else None,
                "old_fact_ids": superseded_ids,
                "old_facts_state": "SUPERSEDED",
                "status": "merged" if saved else "failed",
                "message": f"Merged {len(superseded_ids)} facts" if saved else "Merge failed",
            }
            
        except Exception as e:
            logger.error(f"❌ [FactManagement] Merge facts error: {e}", exc_info=True)
            return {
                "new_fact_id": None,
                "old_fact_ids": [],
                "old_facts_state": "FAILED",
                "status": "failed",
                "message": f"Error merging facts: {str(e)}"
            }

    async def discard_candidate(self, reason: str) -> Dict[str, Any]:
        logger.info(f"🗑️  [FactManagement] Discarded candidate: {reason}")
        return {"status": "discarded", "reason": reason}
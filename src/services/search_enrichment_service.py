import asyncio
from typing import List, Dict, Optional, Union

from ..domain.entities import FactEntity
from ..domain.search import EnrichedContext, EnrichedFact, SearchLimits
from ..domain.vector_math import cosine_similarity
from ..ports.repository import FactRepository
from ..ports.embedding_service import EmbeddingService
from ..ports.search_enrichment_port import SearchEnrichmentPort
from ..utils.logger import logger


class SearchEnrichmentService(SearchEnrichmentPort):
    """
    Multi-vector semantic search with RRF ranking.
    
    Session 2026-02-07: Multi-Vector Semantic Search
    - 6-query strategy: 3 phrases × 2 vector fields each
    - RRF (Reciprocal Rank Fusion) for result merging
    - Configurable limits via total_limit parameter
    """

    def __init__(
        self,
        repository: FactRepository,
        embedding_service: EmbeddingService,
        keyword_limit: int,
        phrase_one_limit: int,
        phrase_two_limit: int,
        total_limit: int
    ) -> None:
        self._repo = repository
        self._embedding = embedding_service
        self._keyword_limit = keyword_limit
        self._phrase_one_limit = phrase_one_limit
        self._phrase_two_limit = phrase_two_limit
        self._total_limit = total_limit
        
        # RRF constant (industry standard)
        self._rrf_k = 60

    async def enrich_context(
        self,
        keywords: List[str],
        search_phrase_1: str,
        search_phrase_2: str,
        relevant_domains: Optional[List[str]] = None,
        biographical_facts: Optional[List[Union[FactEntity, Dict]]] = None,
        limits: Optional[SearchLimits] = None,
        dedup_threshold: float = 0.98,
        skip_semantic_dedup: bool = False,
        sequential: bool = False,
    ) -> EnrichedContext:
        """
        Build enriched context using multi-channel search strategy + RRF ranking.

        Session 2026-02-07: Multi-Vector Semantic Search with RRF
        - 6 parallel queries (3 phrases × 2 vector fields each)
        - Adaptive routing: keywords→tags, phrases→text+metadata
        - RRF (Reciprocal Rank Fusion) for result merging
        - Configurable limit via total_limit

        Session 2026-02-17: Added domain-based direct query channel
        - relevant_domains → direct Firestore query (not vector search)
        - Uses existing index (account_id, domain, created_at)
        - Returns ALL current facts in specified domains
        - Total queries: up to 7 (1 domain + 6 vector)

        SESSION_27: Uses RequestContext for implicit account_id resolution.
        
        Session 2026-02-08: Added limits Override via SearchLimits VO.
        
        Session 2026-02-16: Added configurable dedup_threshold + skip_semantic_dedup
        - 0.98: Default (balanced filtering for READ/WRITE)
        - 1.0: Only exact duplicates (consolidation search mode)
        - skip_semantic_dedup: True = keep ALL facts with different IDs (consolidation MERGE)

        Args:
            keywords: List of semantic lens keywords
            search_phrase_1: Primary search phrase
            search_phrase_2: Secondary search phrase
            relevant_domains: Optional list of 1-3 domains for direct query (NEW)
            biographical_facts: Facts to deduplicate against
            limits: Optional limit overrides (SearchLimits)
            dedup_threshold: Similarity threshold for duplicate detection (default: 0.98)
                - 0.96: Aggressive filtering (remove more duplicates)
                - 0.98: Balanced (default for READ/WRITE)
                - 1.0: Only exact duplicates (consolidation search mode)
            skip_semantic_dedup: Skip semantic deduplication entirely (default: False)
                - False: Normal search (remove semantic duplicates)
                - True: Consolidation mode (keep ALL facts with different IDs for MERGE)
            sequential: Execute Firestore vector queries one-by-one instead of asyncio.gather
                - False (default): Parallel — optimal for conversation path (latency matters)
                - True: Sequential — reduces peak concurrency; use if quota pressure is observed

        Returns:
            EnrichedContext with deduplicated facts

        Strategy:
            - relevant_domains → direct Firestore IN query (precise, all facts in domain)
            - keywords → tags_vector (best for categories) + metadata_vector
            - phrase_1 → vector (text) + tags_vector
            - phrase_2 → vector (text) + metadata_vector
        """
        # Apply limits (override or default)
        effective_limits = limits or SearchLimits(
            keyword_limit=self._keyword_limit,
            phrase_one_limit=self._phrase_one_limit,
            phrase_two_limit=self._phrase_two_limit,
            total_limit=self._total_limit
        )

        # Join keywords into single query
        keyword_query = " ".join([k for k in keywords if k]) if keywords else ""

        # Single batch call: 3 texts → 3 vectors in ~1-2s vs ~15s for 3 parallel to_thread calls.
        # This shrinks the idle window before find_nearest from ~24s to ~11s.
        # Only send non-empty texts — Gemini API rejects empty strings.
        _slots = [keyword_query, search_phrase_1, search_phrase_2]
        _filled = [(i, t) for i, t in enumerate(_slots) if t]
        if _filled:
            _idxs, _txts = zip(*_filled)
            _batch = await self._embedding.get_embeddings_batch(list(_txts), "RETRIEVAL_QUERY")
            _vec = dict(zip(_idxs, _batch))
        else:
            _vec = {}
        keyword_vector = _vec.get(0) if keyword_query else None
        phrase1_vector  = _vec.get(1) if search_phrase_1 else None
        phrase2_vector  = _vec.get(2) if search_phrase_2 else None

        # Up to 7 parallel queries (1 domain + 6 vector)
        search_tasks = []

        # Session 2026-02-17: NEW Channel - Domain-based direct query
        if relevant_domains:
            search_tasks.append(
                self._search_by_domain(relevant_domains, effective_limits.keyword_limit, "domain_direct")
            )

        # Keywords → tags priority (domain nouns match best with tags)
        if keyword_vector:
            search_tasks.append(
                self._search_by_vector_field(keyword_vector, "tags_vector", effective_limits.keyword_limit, "keyword_tags")
            )
            search_tasks.append(
                self._search_by_vector_field(keyword_vector, "metadata_vector", int(effective_limits.keyword_limit * 0.75), "keyword_metadata")
            )

        # Phrase 1 → text priority (natural language)
        if phrase1_vector:
            search_tasks.append(
                self._search_by_vector_field(phrase1_vector, "vector", effective_limits.phrase_one_limit, "phrase1_text")
            )
            search_tasks.append(
                self._search_by_vector_field(phrase1_vector, "tags_vector", int(effective_limits.phrase_one_limit * 0.75), "phrase1_tags")
            )

        # Phrase 2 → balanced text + metadata
        if phrase2_vector:
            search_tasks.append(
                self._search_by_vector_field(phrase2_vector, "vector", int(effective_limits.phrase_two_limit * 0.75), "phrase2_text")
            )
            search_tasks.append(
                self._search_by_vector_field(phrase2_vector, "metadata_vector", effective_limits.phrase_two_limit, "phrase2_metadata")
            )

        # Execute queries: parallel for conversation path, sequential for consolidation
        if sequential:
            logger.info(
                f"🔍 [SearchEnrichment] Sequential mode: executing {len(search_tasks)} queries one-by-one"
            )
            results = []
            for task in search_tasks:
                try:
                    results.append(await task)
                except Exception as exc:
                    results.append(exc)
        else:
            results = await asyncio.gather(*search_tasks, return_exceptions=True)

        # Filter out exceptions
        valid_results = [self._safe_results(r) for r in results]

        logger.info(
            f"🔍 [SearchEnrichment] Executed {len(valid_results)} queries, "
            f"total facts before RRF: {sum(len(r) for r in valid_results)}"
        )

        # Apply RRF ranking
        rrf_ranked = self._apply_rrf_ranking(valid_results, k=self._rrf_k)

        # Session 2026-02-08: Smart semantic deduplication
        # Session 2026-02-16: Now with configurable threshold + skip option
        # Replaces old 3-level dedup (ID + biographical + none)
        # Now uses SmartDeduplication (same as WRITE path)
        
        if skip_semantic_dedup:
            # Consolidation mode: keep ALL facts with different IDs
            # RRF already removed ID duplicates, that's sufficient for MERGE decisions
            final_facts = rrf_ranked[:effective_limits.total_limit]
            dedup_count = 0
            logger.info(
                "🔍 [SearchEnrichment] Semantic dedup SKIPPED (consolidation mode)"
            )
        else:
            # Normal search mode: remove semantic duplicates
            final_facts, dedup_count = await self._deduplicate_semantic(
                rrf_ranked,
                similarity_threshold=dedup_threshold
            )
            # Apply configurable limit
            final_facts = final_facts[:effective_limits.total_limit]

        # Variant B: Remove enriched facts already present in biographical baseline (ID-based)
        # Session 2026-02-17: Fixes duplicate data appearing in both biographical sections
        # and Query-Specific Context. biographical_facts param was accepted but never used.
        biographical_dedup_count = 0
        if biographical_facts:
            bio_ids = set()
            for f in biographical_facts:
                if isinstance(f, dict):
                    fid = f.get("id")
                else:
                    fid = getattr(f, "id", None)
                if fid:
                    bio_ids.add(fid)

            if bio_ids:
                before = len(final_facts)
                final_facts = [f for f in final_facts if f.fact_id not in bio_ids]
                biographical_dedup_count = before - len(final_facts)
                if biographical_dedup_count > 0:
                    logger.info(
                        f"🔍 [SearchEnrichment] Biographical dedup: removed "
                        f"{biographical_dedup_count} facts already in baseline"
                    )

        logger.info(
            f"🔍 [SearchEnrichment] Final: {len(final_facts)} facts "
            f"(semantic_dedup: {dedup_count}, bio_dedup: {biographical_dedup_count}, "
            f"limit: {effective_limits.total_limit})"
        )

        return EnrichedContext(
            facts=final_facts,
            total_sources=len(valid_results),
            dedup_count=dedup_count,
            biographical_dedup_count=biographical_dedup_count
        )

    async def _deduplicate_semantic(
        self,
        facts: List[EnrichedFact],
        similarity_threshold: float = 0.98
    ) -> tuple[List[EnrichedFact], int]:
        """
        Remove ALL duplicates using SmartDeduplication with configurable threshold.
        
        Session 2026-02-08: Unified semantic deduplication for READ path
        - Replaces old _deduplicate_facts() (ID-only) + _deduplicate_biographical() (exact text)
        - Uses same SmartDeduplication as WRITE path (consistency!)
        - Applies 4-level algorithm: similarity, numbers, length, heuristics
        - Now READ and WRITE use identical duplicate detection logic
        
        Session 2026-02-08: Optimization - No redundant Firestore reads
        - EnrichedFact now includes vector (no need to fetch by ID)
        - Eliminates N sequential reads (was causing 150-500ms latency for 50 facts)
        
        Session 2026-02-16: Configurable threshold
        - 0.98: Default (balanced filtering)
        - 1.0: Only exact duplicates (consolidation mode)
        
        Algorithm:
        1. similarity < 0.96 → NOT duplicate (always)
        2. Numbers differ (sorted) → NOT duplicate
        3. similarity < threshold AND new more detailed → NOT duplicate
        4. Otherwise → DUPLICATE
        
        Args:
            facts: EnrichedFacts from RRF ranking (includes vectors)
            similarity_threshold: Threshold for STRICT duplicate detection (default: 0.98)
            
        Returns:
            Tuple (deduplicated_facts, removed_count)
        """
        from ..domain.deduplication_service import SmartDeduplication
        
        if not facts:
            return [], 0
        
        # Create service with custom threshold
        dedup_service = SmartDeduplication(
            moderate_threshold=0.96,  # Always check dissimilar facts
            strict_threshold=similarity_threshold  # Configurable!
        )
        kept: List[EnrichedFact] = []
        removed_count = 0
        
        for enriched in facts:
            # Skip facts without vectors
            if not enriched.vector:
                logger.warning(f"⚠️ Skipping fact {enriched.fact_id} - no vector")
                continue
            
            is_duplicate = False
            
            # Compare with all previously kept facts
            for kept_enriched in kept:
                if not kept_enriched.vector:
                    continue
                
                # Calculate cosine similarity (no Firestore read needed!)
                similarity = cosine_similarity(enriched.vector, kept_enriched.vector)
                
                # Use SmartDeduplication logic (same as WRITE path)
                is_dup, reason = dedup_service.is_duplicate(
                    enriched.content,
                    kept_enriched.content,
                    similarity
                )
                
                if is_dup:
                    is_duplicate = True
                    logger.debug(
                        f"⏭️  [SearchEnrichment] Semantic duplicate: {reason} | "
                        f"sim={similarity:.3f} | fact='{enriched.content[:40]}...'"
                    )
                    break
            
            if not is_duplicate:
                kept.append(enriched)
            else:
                removed_count += 1
        
        if removed_count > 0:
            logger.info(
                f"✅ [SearchEnrichment] Removed {removed_count} semantic duplicates "
                f"({len(kept)} facts remaining)"
            )
        
        return kept, removed_count

    def _safe_results(self, result) -> List[EnrichedFact]:
        if isinstance(result, Exception):
            return []
        return result

    def _apply_rrf_ranking(
        self,
        query_results: List[List[EnrichedFact]],
        k: int = 60
    ) -> List[EnrichedFact]:
        """
        Apply Reciprocal Rank Fusion to merge multi-query results.
        
        Session 2026-02-07: Multi-Vector Semantic Search
        Algorithm: RRF_score(fact) = Σ 1/(k + rank_i)
        
        Industry standard used by: Elasticsearch, Pinecone, Weaviate
        Paper: "Reciprocal Rank Fusion outperforms Condorcet" (Cormack et al., 2009)
        
        Args:
            query_results: List of result lists from each query
            k: RRF constant (default 60, Elasticsearch standard)
        
        Returns:
            Facts sorted by RRF score (descending)
        
        Example:
            Fact appears in 3 queries at ranks [1, 3, 1]:
            RRF = 1/(60+1) + 1/(60+3) + 1/(60+1) = 0.0487
        """
        from collections import defaultdict
        
        # Step 1: Group facts by ID with their ranks
        fact_appearances = defaultdict(list)  # fact_id → [(query_idx, rank, fact), ...]
        
        for query_idx, results in enumerate(query_results):
            for rank, fact in enumerate(results, start=1):
                fact_appearances[fact.fact_id].append((query_idx, rank, fact))
        
        # Step 2: Calculate RRF score for each fact
        scored_facts = []
        
        for fact_id, appearances in fact_appearances.items():
            # RRF formula: sum of 1/(k + rank) across all queries
            rrf_score = sum(1.0 / (k + rank) for _, rank, _ in appearances)
            
            # Take fact from first appearance
            fact = appearances[0][2]
            
            # Store metadata for debugging
            scored_facts.append({
                "fact": fact,
                "rrf_score": rrf_score,
                "appearance_count": len(appearances),
                "ranks": [rank for _, rank, _ in appearances]
            })
        
        # Step 3: Sort by RRF score (descending)
        scored_facts.sort(key=lambda x: x["rrf_score"], reverse=True)
        
        logger.debug(
            f"🔍 [RRF] Ranked {len(scored_facts)} unique facts from {len(query_results)} queries"
        )
        
        # Return just the facts (metadata discarded)
        return [item["fact"] for item in scored_facts]

    async def _search_by_domain(
        self,
        domains: List[str],
        limit: int,
        source_label: str
    ) -> List[EnrichedFact]:
        """
        Search facts by domain using direct Firestore query.
        
        Session 2026-02-17: Domain-based search for router enrichment.
        Uses existing index (account_id, domain, created_at).
        NOT a vector search - returns ALL current facts in specified domains.
        
        Args:
            domains: List of domain values (e.g., ["health", "possession"])
            limit: Max results per domain
            source_label: Label for tracking (e.g., "domain_direct")
        
        Returns:
            List of enriched facts
        """
        try:
            facts = await self._repo.search_facts_by_domain(
                domains=domains,
                limit=limit
            )
            
            return [
                EnrichedFact(
                    fact_id=fact.id,
                    content=fact.text,
                    vector=fact.vector,
                    source=source_label,
                    relevance_score=1.0,
                    fact_type=fact.type.value if fact.type else None,
                    domain=fact.domain.value if fact.domain else None,
                    temporal_class=fact.temporal_class.value if fact.temporal_class else None,
                    state=fact.state.value if fact.state else None,
                    context_priority=fact.context_priority.value if fact.context_priority else None,
                    tags=fact.tags,
                    metadata=fact.metadata,
                    reported_date=fact.reported_date.isoformat() if fact.reported_date else None,
                    context=fact.context,
                    version=fact.version,
                )
                for fact in facts
            ]
        except Exception as exc:
            logger.warning(
                "⚠️ [SearchEnrichmentService] Domain search failed for %s: %s",
                domains,
                exc
            )
            return []

    async def _search_by_vector_field(
        self,
        query_vector: List[float],
        vector_field: str,
        limit: int,
        source_label: str
    ) -> List[EnrichedFact]:
        """
        Search by specific vector field with error handling.
        
        Session 2026-02-07: Multi-Vector Semantic Search
        
        Args:
            query_vector: Query embedding (768 dims)
            vector_field: "vector" | "metadata_vector" | "tags_vector"
            limit: Max results
            source_label: Label for tracking (e.g., "keyword_tags")
        
        Returns:
            List of enriched facts
        """
        try:
            facts = await self._repo.search_facts(
                query_vector=query_vector,
                vector_field=vector_field,
                limit=limit
            )
            
            return [
                EnrichedFact(
                    fact_id=fact.id,
                    content=fact.text,
                    vector=fact.vector,
                    source=source_label,
                    relevance_score=getattr(fact, "similarity", None),
                    fact_type=fact.type.value if fact.type else None,
                    domain=fact.domain.value if fact.domain else None,
                    temporal_class=fact.temporal_class.value if fact.temporal_class else None,
                    state=fact.state.value if fact.state else None,
                    context_priority=fact.context_priority.value if fact.context_priority else None,
                    tags=fact.tags,
                    metadata=fact.metadata,
                    reported_date=fact.reported_date.isoformat() if fact.reported_date else None,
                    context=fact.context,
                    version=fact.version,
                )
                for fact in facts
            ]
        except Exception as exc:
            logger.warning(
                "⚠️ [SearchEnrichmentService] Search failed for %s (%s): %s",
                source_label,
                vector_field,
                exc
            )
            return []

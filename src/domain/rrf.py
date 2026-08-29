"""
Reciprocal Rank Fusion — merges multiple ranked result lists into one.

Session 2026-02-07: extracted from SearchEnrichmentService._apply_rrf_ranking
(originally FactEntity/EnrichedFact-specific) to a generic domain/ function so
both Alek's enrichment and the companion context-assembler (RFC:
COMPANION_AGENTS_RFC.md §6) share one implementation. key_fn is a parameter,
not a hardcoded `.fact_id` attribute — EnrichedFact uses fact_id, CompanionRecord
uses id, and future callers may use neither.

Algorithm: RRF_score(item) = sum(1/(k + rank_i)) across all queries the item
appears in. Industry standard used by Elasticsearch, Pinecone, Weaviate.
Paper: "Reciprocal Rank Fusion outperforms Condorcet" (Cormack et al., 2009).
"""
from collections import defaultdict
from typing import Callable, List, TypeVar

T = TypeVar("T")


def apply_rrf_ranking(
    query_results: List[List[T]],
    key_fn: Callable[[T], str],
    k: int = 60,
) -> List[T]:
    """Merge ranked lists by Reciprocal Rank Fusion, sorted by score descending.

    Args:
        query_results: one ranked list per query.
        key_fn: extracts the dedup/identity key from an item (e.g. lambda f: f.fact_id).
        k: RRF constant (default 60, Elasticsearch standard).
    """
    appearances = defaultdict(list)  # key -> [(rank, item), ...]
    for results in query_results:
        for rank, item in enumerate(results, start=1):
            appearances[key_fn(item)].append((rank, item))

    scored = []
    for key, ranked in appearances.items():
        score = sum(1.0 / (k + rank) for rank, _ in ranked)
        scored.append((score, ranked[0][1]))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored]

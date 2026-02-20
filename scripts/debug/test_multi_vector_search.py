"""
Test multi-vector parallel search strategy.

Single query: "car, vehicle, assets"
- 3 parallel searches (20 results each = 60 total)
- Deduplication by text field
- Ranking by similarity score
- Save results to file
"""
import asyncio
import sys
import os
import math
import json
from datetime import datetime
from typing import List, Dict, Any
from dataclasses import dataclass, asdict

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector
from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig
from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter


@dataclass
class SearchResult:
    """Search result with metadata."""
    id: str
    text: str
    similarity: float
    source: str  # 'text', 'metadata', or 'tags'
    type: str
    tags: List[str]
    metadata: Dict[str, Any]


def cosine_similarity(vec1, vec2):
    """Calculate cosine similarity."""
    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    magnitude1 = math.sqrt(sum(a * a for a in vec1))
    magnitude2 = math.sqrt(sum(b * b for b in vec2))
    if magnitude1 == 0 or magnitude2 == 0:
        return 0
    return dot_product / (magnitude1 * magnitude2)


async def search_by_vector_field(
    db: firestore.AsyncClient,
    collection_name: str,
    account_id: str,
    query_vector: List[float],
    vector_field: str,
    limit: int = 20
) -> List[SearchResult]:
    """Search by specific vector field."""
    
    try:
        # Build query
        query = (
            db.collection(collection_name)
            .where(filter=FieldFilter("account_id", "==", account_id))
            .where(filter=FieldFilter("is_current", "==", True))
            .find_nearest(
                vector_field=vector_field,
                query_vector=Vector(query_vector),
                distance_measure=DistanceMeasure.COSINE,
                limit=limit
            )
        )
        
        # Execute
        docs = [doc async for doc in query.stream()]
        
        # Convert to results
        results = []
        for doc in docs:
            data = doc.to_dict()
            
            # Get vector for similarity calculation
            vector = data.get(vector_field)
            if isinstance(vector, Vector):
                vector = list(vector)
            
            similarity = cosine_similarity(query_vector, vector) if vector else 0.0
            
            results.append(SearchResult(
                id=doc.id,
                text=data.get("text", ""),
                similarity=similarity,
                source=vector_field.replace("_vector", "").replace("vector", "text"),
                type=str(data.get("type", "unknown")),
                tags=data.get("tags", []),
                metadata=data.get("metadata", {})
            ))
        
        return results
    
    except Exception as e:
        print(f"   ⚠️  Error searching {vector_field}: {e}")
        return []


async def parallel_multi_vector_search(
    db: firestore.AsyncClient,
    embedding_service: GeminiEmbeddingAdapter,
    collection_name: str,
    account_id: str,
    query: str,
    limit_per_vector: int = 20
) -> Dict[str, Any]:
    """Perform parallel search across 3 vector fields."""
    
    print(f"\n{'=' * 80}")
    print(f"🔍 Query: \"{query}\"")
    print(f"{'=' * 80}")
    
    # Generate query embedding
    print(f"\n1️⃣  Generating query embedding...")
    query_vector = await embedding_service.get_embedding(query, task_type="RETRIEVAL_QUERY")
    print(f"   ✅ Generated ({len(query_vector)} dimensions)")
    
    # Parallel searches
    print(f"\n2️⃣  Performing 3 parallel vector searches (limit={limit_per_vector} each)...")
    
    text_results, metadata_results, tags_results = await asyncio.gather(
        search_by_vector_field(db, collection_name, account_id, query_vector, "vector", limit_per_vector),
        search_by_vector_field(db, collection_name, account_id, query_vector, "metadata_vector", limit_per_vector),
        search_by_vector_field(db, collection_name, account_id, query_vector, "tags_vector", limit_per_vector)
    )
    
    print(f"   ✅ Text vector: {len(text_results)} results")
    print(f"   ✅ Metadata vector: {len(metadata_results)} results")
    print(f"   ✅ Tags vector: {len(tags_results)} results")
    
    # Deduplication by text field
    print(f"\n3️⃣  Deduplicating results by text field...")
    
    seen_texts = set()
    deduplicated = []
    
    # Merge all results
    all_results = text_results + metadata_results + tags_results
    
    # Sort by similarity first (RANKING BY VECTOR SIMILARITY)
    all_results.sort(key=lambda r: r.similarity, reverse=True)
    
    for result in all_results:
        if result.text not in seen_texts:
            seen_texts.add(result.text)
            deduplicated.append(result)
    
    print(f"   ✅ Total before dedup: {len(all_results)}")
    print(f"   ✅ After dedup: {len(deduplicated)} unique facts")
    print(f"   ✅ Duplicates removed: {len(all_results) - len(deduplicated)}")
    
    return {
        "query": query,
        "timestamp": datetime.now().isoformat(),
        "text_results": text_results,
        "metadata_results": metadata_results,
        "tags_results": tags_results,
        "deduplicated": deduplicated,
        "total_before_dedup": len(all_results),
        "total_after_dedup": len(deduplicated),
        "duplicates_removed": len(all_results) - len(deduplicated)
    }


def save_results_to_file(search_data: Dict[str, Any], output_file: str):
    """Save search results to JSON file."""
    
    # Convert SearchResult objects to dicts
    def convert_result(result):
        if isinstance(result, SearchResult):
            return asdict(result)
        return result
    
    output_data = {
        "query": search_data["query"],
        "timestamp": search_data["timestamp"],
        "statistics": {
            "total_before_dedup": search_data["total_before_dedup"],
            "total_after_dedup": search_data["total_after_dedup"],
            "duplicates_removed": search_data["duplicates_removed"]
        },
        "text_results": [convert_result(r) for r in search_data["text_results"]],
        "metadata_results": [convert_result(r) for r in search_data["metadata_results"]],
        "tags_results": [convert_result(r) for r in search_data["tags_results"]],
        "deduplicated_ranked": [convert_result(r) for r in search_data["deduplicated"]]
    }
    
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, indent=2, ensure_ascii=False)
    
    print(f"\n💾 Results saved to: {output_file}")


def print_results(search_data: Dict[str, Any], top_n: int = 10):
    """Print search results in a nice format."""
    
    print(f"\n4️⃣  Top Results by Vector Source:")
    
    print(f"\n" + "─" * 80)
    print(f"📊 TEXT VECTOR RESULTS (Top 10):")
    print(f"─" * 80)
    
    for i, result in enumerate(search_data["text_results"][:10], 1):
        print(f"\n[{i}] Similarity: {result.similarity:.4f}")
        print(f"    ID: {result.id[:30]}...")
        print(f"    Type: {result.type}")
        print(f"    Tags: {result.tags}")
        print(f"    Text: {result.text[:100]}...")
    
    print(f"\n" + "─" * 80)
    print(f"📊 METADATA VECTOR RESULTS (Top 10):")
    print(f"─" * 80)
    
    for i, result in enumerate(search_data["metadata_results"][:10], 1):
        print(f"\n[{i}] Similarity: {result.similarity:.4f}")
        print(f"    ID: {result.id[:30]}...")
        print(f"    Type: {result.type}")
        print(f"    Metadata: {result.metadata}")
        print(f"    Text: {result.text[:100]}...")
    
    print(f"\n" + "─" * 80)
    print(f"📊 TAGS VECTOR RESULTS (Top 10):")
    print(f"─" * 80)
    
    for i, result in enumerate(search_data["tags_results"][:10], 1):
        print(f"\n[{i}] Similarity: {result.similarity:.4f}")
        print(f"    ID: {result.id[:30]}...")
        print(f"    Type: {result.type}")
        print(f"    Tags: {result.tags}")
        print(f"    Text: {result.text[:100]}...")
    
    print(f"\n" + "=" * 80)
    print(f"🎯 DEDUPLICATED & RANKED BY SIMILARITY (Top {top_n}):")
    print(f"=" * 80)
    
    for i, result in enumerate(search_data["deduplicated"][:top_n], 1):
        print(f"\n[{i}] Similarity: {result.similarity:.4f} | Source: {result.source}")
        print(f"    ID: {result.id[:30]}...")
        print(f"    Type: {result.type}")
        print(f"    Tags: {result.tags}")
        print(f"    Text: {result.text[:150]}...")
        if result.metadata:
            print(f"    Metadata: {result.metadata}")
    
    print(f"\n" + "=" * 80)
    print(f"📈 STATISTICS:")
    print(f"=" * 80)
    print(f"   Total results (before dedup): {search_data['total_before_dedup']}")
    print(f"   Unique results (after dedup): {search_data['total_after_dedup']}")
    print(f"   Duplicates removed: {search_data['duplicates_removed']}")
    
    # Source distribution
    source_counts = {}
    for result in search_data["deduplicated"][:top_n]:
        source_counts[result.source] = source_counts.get(result.source, 0) + 1
    
    print(f"\n   Source distribution (top {top_n}):")
    for source, count in sorted(source_counts.items(), key=lambda x: x[1], reverse=True):
        print(f"      {source}: {count} results")


async def main():
    """Run multi-vector search test with single query."""
    
    print("=" * 80)
    print("🚀 MULTI-VECTOR PARALLEL SEARCH TEST")
    print("=" * 80)
    print("Query: 'car, vehicle, assets'")
    print("Strategy: 3 vectors × 20 results = 60 → deduplicate → rank by similarity")
    print("=" * 80)
    
    # Setup
    config = load_settings()
    env_config = EnvironmentConfig()
    db_id = env_config.firestore_database_id
    collection_name = env_config.domain_facts_collection
    
    print(f"🟢 {env_config.env.value.upper()} MODE")
    print(f"\n📊 Configuration:")
    print(f"   Database: {db_id}")
    print(f"   Collection: {collection_name}")
    
    # Initialize services
    db = firestore.AsyncClient(
        project=config["GOOGLE_CLOUD_PROJECT"],
        database=db_id
    )
    
    embedding_service = GeminiEmbeddingAdapter(api_key=config["GEMINI_API_KEY"])
    
    # Test account
    user_id = os.getenv("USER_ID") or "DEMO_USER"
    account_id = f"account-{user_id}"
    
    # Single query as requested
    query = "car, vehicle, assets"
    
    # Run search
    result = await parallel_multi_vector_search(
        db=db,
        embedding_service=embedding_service,
        collection_name=collection_name,
        account_id=account_id,
        query=query,
        limit_per_vector=20
    )
    
    # Print results
    print_results(result, top_n=20)
    
    # Save to file
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = f"reports/vector_search_results_{timestamp}.json"
    
    # Ensure reports directory exists
    os.makedirs("reports", exist_ok=True)
    
    save_results_to_file(result, output_file)
    
    print(f"\n{'=' * 80}")
    print(f"✅ TEST COMPLETE")
    print(f"{'=' * 80}")
    print(f"📊 Summary:")
    print(f"   Query: \"{query}\"")
    print(f"   Total collected: {result['total_before_dedup']} results (3 × 20)")
    print(f"   After deduplication: {result['total_after_dedup']} unique facts")
    print(f"   Duplicates removed: {result['duplicates_removed']}")
    print(f"   Best similarity: {result['deduplicated'][0].similarity:.4f} (from {result['deduplicated'][0].source})")
    print(f"   Output file: {output_file}")
    print(f"{'=' * 80}")


if __name__ == "__main__":
    asyncio.run(main())

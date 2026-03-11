#!/usr/bin/env python3
"""
Benchmark script for comparing flat vs approximate vector indexes.

Usage:
    python scripts/debug/benchmark_vector_index.py --field tags_vector
    python scripts/debug/benchmark_vector_index.py --field metadata_vector
    python scripts/debug/benchmark_vector_index.py --field vector
"""
import asyncio
import time
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.adapters.firestore_repo import FirestoreFactRepository
from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
from src.config.environment import EnvironmentConfig


async def benchmark_field(field_name: str, queries: list[str], limit: int = 20):
    """
    Benchmark vector search performance for a specific field.
    
    Args:
        field_name: "vector", "tags_vector", or "metadata_vector"
        queries: List of search queries
        limit: Number of results to retrieve
    """
    print(f"\n{'='*60}")
    print(f"🔍 Benchmarking: {field_name}")
    print(f"{'='*60}\n")
    
    # Initialize services
    config = EnvironmentConfig()
    embedding_service = GeminiEmbeddingAdapter(api_key=config.gemini_api_key)
    repo = FirestoreFactRepository(
        project_id=config.project_id,
        database_id=config.firestore_database_id,
        collection_prefix=config.collection_prefix
    )
    
    results_summary = []
    
    for query_text in queries:
        print(f"Query: '{query_text}'")
        
        try:
            # Generate embedding
            embedding_start = time.time()
            query_embedding = await embedding_service.get_embedding(
                query_text, 
                task_type="RETRIEVAL_QUERY"
            )
            embedding_time = time.time() - embedding_start
            
            # Execute search
            search_start = time.time()
            
            # Determine which search method to use
            if field_name == "tags_vector":
                results = await repo.search_by_tags_vector(
                    query_vector=query_embedding,
                    limit=limit
                )
            elif field_name == "metadata_vector":
                results = await repo.search_by_metadata_vector(
                    query_vector=query_embedding,
                    limit=limit
                )
            else:  # "vector" (main field)
                results = await repo.search_by_vector(
                    query_vector=query_embedding,
                    limit=limit
                )
            
            search_time = time.time() - search_start
            total_time = embedding_time + search_time
            
            # Analyze results
            num_results = len(results)
            top_similarity = results[0].similarity if results else 0.0
            avg_similarity = sum(r.similarity for r in results[:10]) / min(10, num_results) if results else 0.0
            
            print(f"  ⏱️  Embedding time: {embedding_time:.3f}s")
            print(f"  🔍 Search time:    {search_time:.3f}s")
            print(f"  📊 Total time:     {total_time:.3f}s")
            print(f"  📈 Results:        {num_results}")
            print(f"  🎯 Top similarity: {top_similarity:.4f}")
            print(f"  📊 Avg similarity: {avg_similarity:.4f} (top 10)")
            print()
            
            results_summary.append({
                "query": query_text,
                "total_time": total_time,
                "search_time": search_time,
                "num_results": num_results,
                "top_similarity": top_similarity,
                "avg_similarity": avg_similarity
            })
            
        except Exception as e:
            print(f"  ❌ Error: {e}")
            print()
    
    # Summary statistics
    if results_summary:
        print(f"\n{'='*60}")
        print(f"📊 Summary for {field_name}")
        print(f"{'='*60}\n")
        
        avg_total = sum(r["total_time"] for r in results_summary) / len(results_summary)
        avg_search = sum(r["search_time"] for r in results_summary) / len(results_summary)
        avg_results = sum(r["num_results"] for r in results_summary) / len(results_summary)
        avg_top_sim = sum(r["top_similarity"] for r in results_summary) / len(results_summary)
        avg_avg_sim = sum(r["avg_similarity"] for r in results_summary) / len(results_summary)
        
        print(f"Average total time:     {avg_total:.3f}s")
        print(f"Average search time:    {avg_search:.3f}s")
        print(f"Average results:        {avg_results:.1f}")
        print(f"Average top similarity: {avg_top_sim:.4f}")
        print(f"Average avg similarity: {avg_avg_sim:.4f}")
        print()


async def main():
    """Run benchmark tests."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Benchmark Firestore vector indexes")
    parser.add_argument(
        "--field",
        choices=["vector", "tags_vector", "metadata_vector", "all"],
        default="tags_vector",
        help="Which vector field to benchmark"
    )
    parser.add_argument(
        "--queries",
        nargs="+",
        default=["car vehicle assets", "travel flight hotel", "health medical analysis"],
        help="Search queries to test"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Number of results to retrieve"
    )
    
    args = parser.parse_args()
    
    if args.field == "all":
        fields = ["tags_vector", "metadata_vector", "vector"]
    else:
        fields = [args.field]
    
    for field in fields:
        await benchmark_field(field, args.queries, args.limit)


if __name__ == "__main__":
    asyncio.run(main())

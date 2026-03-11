import asyncio
import time
import os
import random
from typing import List
from google.cloud import firestore
from google.cloud.firestore_v1.vector import Vector
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure

# Initialize Firestore
db = firestore.AsyncClient(database="us-production")
collection_name = "development_domain_facts_v2"
collection = db.collection(collection_name)

async def test_search(use_vector_wrapper: bool, query_num: int):
    """Perform a vector search and measure latency."""
    # Random query vector (simulated)
    query_vector = [random.random() for _ in range(768)]
    
    # We need a valid account_id for the query to match existing indexes
    # Using a dummy account_id or one found in existing docs would be better,
    # but for testing index selection, any string will do (it might return 0 results but will use the index)
    test_account_id = "test_account_latency_check"
    
    start_time = time.time()
    
    try:
        # Prepare query vector based on wrapper usage
        vector_param = Vector(query_vector) if use_vector_wrapper else query_vector
        
        # Execute query
        # Note: We filter by is_current=True AND account_id as per production code
        query = collection.where("account_id", "==", test_account_id).where("is_current", "==", True).find_nearest(
            vector_field="vector",
            query_vector=vector_param,
            distance_measure=DistanceMeasure.COSINE,
            limit=10
        )
        
        results = await query.get()
        
        end_time = time.time()
        duration_ms = (end_time - start_time) * 1000
        
        print(f"Query {query_num}: {duration_ms:.2f}ms (Wrapper: {use_vector_wrapper}, Results: {len(results)})")
        return duration_ms
        
    except Exception as e:
        print(f"Query {query_num} FAILED: {e}")
        return 0

async def main():
    print(f"🚀 Diagnosis: Firestore Vector Search Latency")
    print(f"Collection: {collection_name}")
    print("-" * 50)

    # 1. Warm-up
    print("\n🔥 Warming up...")
    await test_search(use_vector_wrapper=True, query_num=0)

    # 2. Sequential execution with Vector() wrapper (Current production behavior)
    print("\n🐢 Sequential Execution (with Vector wrapper):")
    total_seq_wrapper = 0
    for i in range(1, 4):
        duration = await test_search(use_vector_wrapper=True, query_num=i)
        total_seq_wrapper += duration
    print(f"Avg Sequential (Wrapper): {total_seq_wrapper/3:.2f}ms")

    # 3. Parallel execution with Vector() wrapper (Should match production parallelism)
    print("\n🐢 Parallel Execution (with Vector wrapper):")
    start_time = time.time()
    tasks = [test_search(use_vector_wrapper=True, query_num=i) for i in range(4, 7)]
    await asyncio.gather(*tasks)
    total_time = (time.time() - start_time) * 1000
    print(f"Total Parallel Time (Wrapper): {total_time:.2f}ms")

    # 4. Sequential execution WITHOUT Vector() wrapper (Proposed fix)
    print("\n⚡ Sequential Execution (WITHOUT Vector wrapper):")
    total_seq_no_wrapper = 0
    for i in range(7, 10):
        duration = await test_search(use_vector_wrapper=False, query_num=i)
        total_seq_no_wrapper += duration
    print(f"Avg Sequential (No Wrapper): {total_seq_no_wrapper/3:.2f}ms")

    # 5. Parallel execution WITHOUT Vector() wrapper (Proposed fix)
    print("\n⚡ Parallel Execution (WITHOUT Vector wrapper):")
    start_time = time.time()
    tasks = [test_search(use_vector_wrapper=False, query_num=i) for i in range(10, 13)]
    await asyncio.gather(*tasks)
    total_time = (time.time() - start_time) * 1000
    print(f"Total Parallel Time (No Wrapper): {total_time:.2f}ms")

if __name__ == "__main__":
    asyncio.run(main())

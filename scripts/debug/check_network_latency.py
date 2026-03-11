import asyncio
import time
import os
import random
from typing import List
from google.cloud import firestore
from google.cloud.firestore import FieldFilter

# Initialize Firestore
db = firestore.AsyncClient(database="us-production")
collection_name = "development_domain_facts_v2"
collection = db.collection(collection_name)

async def test_simple_read(query_num: int):
    """Test simple document read latency (get by ID)."""
    start_time = time.time()
    try:
        # Just pick a known existing doc if possible, or query limit 1
        docs = await collection.limit(1).get()
        if docs:
            doc_id = docs[0].id
            # Now measure single doc get
            t0 = time.time()
            await collection.document(doc_id).get()
            latency = (time.time() - t0) * 1000
            print(f"📖 Simple Read (Get by ID) {query_num}: {latency:.2f}ms")
            return latency
        return 0
    except Exception as e:
        print(f"Read {query_num} FAILED: {e}")
        return 0

async def test_simple_query(query_num: int):
    """Test simple field query latency (NO VECTOR)."""
    # We need a valid account_id for the query to match existing indexes
    test_account_id = "test_account_latency_check"
    
    start_time = time.time()
    try:
        # Simple filter query (account_id + is_current)
        query = collection.where(filter=FieldFilter("account_id", "==", test_account_id)).where(filter=FieldFilter("is_current", "==", True)).limit(10)
        results = await query.get()
        latency = (time.time() - start_time) * 1000
        print(f"🔍 Simple Query (Filter) {query_num}: {latency:.2f}ms (Results: {len(results)})")
        return latency
    except Exception as e:
        print(f"Query {query_num} FAILED: {e}")
        return 0

async def main():
    print(f"🚀 Network Latency Check (Spain -> us-central1)")
    print(f"Database: us-production")
    print("-" * 50)

    # 1. Warm-up
    print("\n🔥 Warming up...")
    await test_simple_read(0)

    # 2. Simple Reads (Get by ID) - Baseline Network Latency
    print("\n📖 Testing Simple Read (Get by ID) - Baseline Network Latency:")
    total_read = 0
    for i in range(1, 6):
        total_read += await test_simple_read(i)
    print(f"Avg Simple Read: {total_read/5:.2f}ms")

    # 3. Simple Queries (Filter only) - Index Scan Latency
    print("\n🔍 Testing Simple Query (Filter only, NO vector):")
    total_query = 0
    for i in range(1, 6):
        total_query += await test_simple_query(i)
    print(f"Avg Simple Query: {total_query/5:.2f}ms")

if __name__ == "__main__":
    asyncio.run(main())

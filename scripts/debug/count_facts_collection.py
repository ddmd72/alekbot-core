"""
Count documents in development_domain_facts_v2 collection.

Usage:
    python scripts/debug/count_facts_collection.py
"""

import asyncio
from google.cloud import firestore

async def count_collection_docs():
    """Count all documents in development_domain_facts_v2 collection."""
    
    # Initialize Firestore client with us-production database
    db = firestore.AsyncClient(database="us-production")
    
    collection_name = "development_domain_facts_v2"
    
    print(f"📊 Counting documents in: {collection_name}")
    print(f"📂 Database: us-production")
    print("⏳ Please wait...")
    print()
    
    # Get collection reference
    collection_ref = db.collection(collection_name)
    
    # Count all documents
    docs = collection_ref.stream()
    
    count = 0
    async for doc in docs:
        count += 1
        if count % 100 == 0:
            print(f"   Counted: {count} documents...", end="\r")
    
    print()
    print(f"✅ Total documents: {count}")
    print()
    
    # Get some additional stats
    print("📈 Additional stats:")
    
    # Count by is_current
    current_docs = collection_ref.where(filter=firestore.FieldFilter("is_current", "==", True)).stream()
    current_count = 0
    async for doc in current_docs:
        current_count += 1
    
    archived_count = count - current_count
    
    print(f"   Current facts (is_current=True): {current_count}")
    print(f"   Archived facts (is_current=False): {archived_count}")
    print()

if __name__ == "__main__":
    asyncio.run(count_collection_docs())

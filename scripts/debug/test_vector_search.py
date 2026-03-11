"""
Debug script to test vector search for specific query.
Tests if Memory Search can find specific facts.
"""
import asyncio
import sys
import os

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from google.cloud import firestore
from src.config.settings import load_settings
from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
from src.adapters.firestore_repo import FirestoreFactRepository
from src.config.environment import EnvironmentConfig


async def test_vector_search(query: str, account_id: str, target_fact_id: str = None):
    """Test vector search for a specific query."""
    print(f"\n🔍 Testing vector search for: '{query}'")
    print(f"📁 Account: {account_id}")
    if target_fact_id:
        print(f"🎯 Looking for fact: {target_fact_id}")
    print("=" * 80)
    
    # Setup
    config = load_settings()
    env_config = EnvironmentConfig()
    db_id = env_config.firestore_database_id
    
    print(f"🗄️  Database: {db_id}")
    
    db = firestore.AsyncClient(
        project=config["GOOGLE_CLOUD_PROJECT"],
        database=db_id
    )
    
    # Initialize embedding service
    embedding_service = GeminiEmbeddingAdapter(api_key=config["GEMINI_API_KEY"])
    
    # Initialize repository
    repo = FirestoreFactRepository(db, env_config, embedding_service)
    await repo.initialize()
    
    # Generate embedding
    print(f"\n1️⃣ Generating embedding for query...")
    query_vector = await embedding_service.get_embedding(query, task_type="RETRIEVAL_QUERY")
    print(f"   ✓ Embedding generated (dim={len(query_vector)})")
    
    # Search
    print(f"\n2️⃣ Performing vector search...")
    
    # Use RequestContext to set account_id
    from src.domain.request_context import RequestContext
    async with RequestContext(user_id=account_id.replace("account-", ""), account_id=account_id):
        facts = await repo.search_facts(query_vector, limit=10)
    
    print(f"   ✓ Found {len(facts)} results")
    
    # Display results
    print(f"\n3️⃣ Top {len(facts)} results:")
    print("-" * 80)
    
    found_target = False
    for i, fact in enumerate(facts, 1):
        is_target = fact.id == target_fact_id if target_fact_id else False
        marker = "🎯 TARGET! " if is_target else ""
        
        print(f"\n{marker}[{i}] ID: {fact.id}")
        print(f"    Type: {fact.type}")
        print(f"    Tags: {fact.tags}")
        print(f"    Text: {fact.text[:100]}...")
        
        if is_target:
            found_target = True
    
    print("-" * 80)
    
    # Summary
    print(f"\n4️⃣ Summary:")
    print(f"   Total results: {len(facts)}")
    if target_fact_id:
        if found_target:
            print(f"   ✅ Target fact FOUND in results!")
        else:
            print(f"   ❌ Target fact NOT FOUND in top-10 results")
            print(f"   💡 Fact might be outside top-10 or has no vector")


async def main():
    # Test parameters - Search for facts about car
    queries = [
        "Toyota Corolla car",
        "vehicle VIN number",
        "license plate XX0000YY"
    ]
    
    # This is the account_id stored in Firestore (WITH prefix now after fix!)
    user_id = os.getenv("USER_ID") or "DEMO_USER"
    account_id = f"account-{user_id}"
    
    # Test with ID that HAS correct account_id format
    # Pick any recent fact ID for testing
    target_fact_id = None  # Just search, don't look for specific ID
    
    for query in queries:
        await test_vector_search(query, account_id, target_fact_id)
        print("\n" + "=" * 80 + "\n")


if __name__ == "__main__":
    asyncio.run(main())

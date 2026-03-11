"""
Deep diagnosis: why specific fact doesn't appear in vector search results.
"""
import asyncio
import sys
import os
import math

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from google.cloud import firestore
from google.cloud.firestore_v1.vector import Vector
from src.config.settings import load_settings
from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
from src.adapters.firestore_repo import FirestoreFactRepository
from src.config.environment import EnvironmentConfig


def cosine_similarity(vec1, vec2):
    """Calculate cosine similarity between two vectors."""
    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    magnitude1 = math.sqrt(sum(a * a for a in vec1))
    magnitude2 = math.sqrt(sum(b * b for b in vec2))
    if magnitude1 == 0 or magnitude2 == 0:
        return 0
    return dot_product / (magnitude1 * magnitude2)


async def diagnose_fact_search(fact_id: str, query: str, account_id: str):
    """Deep diagnosis of why fact doesn't appear in search."""
    print(f"\n🔬 DEEP DIAGNOSIS")
    print("=" * 80)
    print(f"🎯 Target Fact: {fact_id}")
    print(f"🔍 Query: '{query}'")
    print(f"📁 Account: {account_id}")
    print("=" * 80)
    
    # Setup
    config = load_settings()
    env_config = EnvironmentConfig()
    db_id = env_config.firestore_database_id
    collection_name = env_config.domain_facts_collection
    
    print(f"\n🗄️  Environment:")
    print(f"   Database: {db_id}")
    print(f"   Collection: {collection_name}")
    print(f"   Is Production: {env_config.is_production}")
    
    db = firestore.AsyncClient(
        project=config["GOOGLE_CLOUD_PROJECT"],
        database=db_id
    )
    
    # Initialize embedding service
    embedding_service = GeminiEmbeddingAdapter(api_key=config["GEMINI_API_KEY"])
    
    # Initialize repository
    repo = FirestoreFactRepository(db, env_config, embedding_service)
    await repo.initialize()
    
    # ========== STEP 1: Get Target Fact ==========
    print(f"\n" + "=" * 80)
    print("STEP 1: Verify Target Fact Exists")
    print("=" * 80)
    
    doc_ref = db.collection(collection_name).document(fact_id)
    doc = await doc_ref.get()
    
    if not doc.exists:
        print(f"❌ CRITICAL: Fact NOT FOUND in Firestore!")
        print(f"   Fact ID: {fact_id}")
        print(f"   Collection: {collection_name}")
        print(f"   Database: {db_id}")
        return
    
    fact_data = doc.to_dict()
    
    print(f"✅ Fact exists!")
    print(f"\n📊 Fact Details:")
    print(f"   ID: {fact_id}")
    print(f"   Account ID: {fact_data.get('account_id', 'N/A')}")
    print(f"   Type: {fact_data.get('type', 'N/A')}")
    print(f"   Tags: {fact_data.get('tags', [])}")
    print(f"   is_current: {fact_data.get('is_current', False)}")
    print(f"   Text: {fact_data.get('text', 'N/A')[:150]}...")
    
    # Check account_id format
    fact_account_id = fact_data.get('account_id', '')
    if fact_account_id != account_id:
        print(f"\n⚠️  WARNING: Account ID mismatch!")
        print(f"   Expected: {account_id}")
        print(f"   Found:    {fact_account_id}")
        print(f"   → This fact will be FILTERED OUT by pre-filter!")
    else:
        print(f"\n✅ Account ID matches: {account_id}")
    
    # Check is_current
    if not fact_data.get('is_current', False):
        print(f"\n❌ CRITICAL: is_current = False")
        print(f"   → This fact will be FILTERED OUT by pre-filter!")
        return
    else:
        print(f"✅ is_current = True")
    
    # Check vector
    has_vector = 'vector' in fact_data
    if not has_vector:
        print(f"\n❌ CRITICAL: NO VECTOR!")
        print(f"   → This fact CANNOT be found by vector search!")
        print(f"   → Needs re-consolidation to generate embedding")
        return
    
    fact_vector = fact_data['vector']
    if isinstance(fact_vector, Vector):
        fact_vector = list(fact_vector)
    
    print(f"✅ Vector exists (dim={len(fact_vector)})")
    
    # ========== STEP 2: Generate Query Embedding ==========
    print(f"\n" + "=" * 80)
    print("STEP 2: Generate Query Embedding")
    print("=" * 80)
    
    print(f"Generating embedding for: '{query}'")
    query_vector = await embedding_service.get_embedding(query, task_type="RETRIEVAL_QUERY")
    print(f"✅ Query embedding generated (dim={len(query_vector)})")
    
    # ========== STEP 3: Calculate Cosine Similarity ==========
    print(f"\n" + "=" * 80)
    print("STEP 3: Calculate Cosine Similarity")
    print("=" * 80)
    
    similarity = cosine_similarity(query_vector, fact_vector)
    distance = 1.0 - similarity
    
    print(f"📐 Similarity Metrics:")
    print(f"   Cosine Similarity: {similarity:.6f}")
    print(f"   Cosine Distance:   {distance:.6f}")
    print(f"   Match Quality:     ", end="")
    
    if similarity > 0.9:
        print("🔥 EXCELLENT (>0.9)")
    elif similarity > 0.8:
        print("✅ VERY GOOD (0.8-0.9)")
    elif similarity > 0.7:
        print("👍 GOOD (0.7-0.8)")
    elif similarity > 0.6:
        print("⚠️  FAIR (0.6-0.7)")
    else:
        print("❌ POOR (<0.6)")
    
    # ========== STEP 4: Perform Actual Vector Search ==========
    print(f"\n" + "=" * 80)
    print("STEP 4: Perform Vector Search (Top 10)")
    print("=" * 80)
    
    from src.domain.request_context import RequestContext
    async with RequestContext(user_id=account_id.replace("account-", ""), account_id=account_id):
        facts = await repo.search_facts(query_vector, limit=10)
    
    print(f"✅ Found {len(facts)} results")
    
    # Check if target fact is in results
    target_found = False
    target_rank = None
    
    for i, fact in enumerate(facts, 1):
        if fact.id == fact_id:
            target_found = True
            target_rank = i
            break
    
    if target_found:
        print(f"\n🎯 ✅ TARGET FACT FOUND!")
        print(f"   Rank: #{target_rank} out of {len(facts)}")
        print(f"   Similarity: {similarity:.6f}")
    else:
        print(f"\n🎯 ❌ TARGET FACT NOT FOUND in top-10!")
        print(f"   Expected similarity: {similarity:.6f}")
        print(f"   This means other facts have higher similarity.")
    
    # ========== STEP 5: Show Top 10 Results ==========
    print(f"\n" + "=" * 80)
    print("STEP 5: Top 10 Results Comparison")
    print("=" * 80)
    
    for i, fact in enumerate(facts, 1):
        is_target = fact.id == fact_id
        marker = "🎯 " if is_target else "   "
        
        # Calculate similarity for this result
        result_vector = fact.vector
        if result_vector:
            result_similarity = cosine_similarity(query_vector, result_vector)
        else:
            result_similarity = 0.0
        
        print(f"\n{marker}[{i}] Similarity: {result_similarity:.6f}")
        print(f"   ID: {fact.id[:20]}...")
        print(f"   Type: {fact.type}")
        print(f"   Text: {fact.text[:80]}...")
    
    # ========== STEP 6: Diagnosis Summary ==========
    print(f"\n" + "=" * 80)
    print("STEP 6: Diagnosis Summary")
    print("=" * 80)
    
    if target_found:
        print(f"✅ RESULT: Fact IS FOUND in vector search (rank #{target_rank})")
        print(f"✅ No bug detected - fact appears in results as expected.")
    else:
        print(f"❌ RESULT: Fact NOT FOUND in top-10")
        print(f"\n🔍 Possible Causes:")
        
        if similarity < 0.7:
            print(f"   1. ⚠️  LOW SEMANTIC SIMILARITY ({similarity:.6f})")
            print(f"      → Query text and fact text are semantically distant")
            print(f"      → Consider: different query terms, enriched fact text, or re-embedding")
        else:
            print(f"   1. ✅ Semantic similarity is acceptable ({similarity:.6f})")
        
        print(f"\n   2. 🔍 OTHER FACTS RANK HIGHER")
        print(f"      → Check top-10 results above")
        print(f"      → See what facts Firestore prefers over target fact")
        
        print(f"\n   3. 🗄️  FIRESTORE INDEX ISSUE")
        print(f"      → Check Firestore Console → Indexes")
        print(f"      → Verify vector index status for {collection_name}")
        print(f"      → Index might be stale or building")
        
        print(f"\n   4. 📊 VECTOR QUALITY")
        print(f"      → Fact vector might be poorly generated")
        print(f"      → Consider re-consolidation to regenerate embedding")


async def main():
    # Test parameters
    fact_id = "bbb03e6c-649f-4cf1-b681-51156752ea20"
    query = "Toyota Corolla car"
    user_id = os.getenv("USER_ID") or "DEMO_USER"
    account_id = f"account-{user_id}"
    
    await diagnose_fact_search(fact_id, query, account_id)
    
    print(f"\n" + "=" * 80)
    print("DIAGNOSIS COMPLETE")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())

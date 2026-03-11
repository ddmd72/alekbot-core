import asyncio
import os
import sys
import time
from pathlib import Path
from collections import Counter

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from google.cloud import firestore
from src.config.settings import load_settings
from src.services.embedding_service import EmbeddingService


async def test_semantic_search():
    """Test optimized semantic biographical search."""
    
    # Setup
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]
    
    db_client = firestore.AsyncClient(project=config.get("GOOGLE_CLOUD_PROJECT"))
    embedding_service = EmbeddingService(api_key=config["GEMINI_API_KEY"])
    
    # USER ID
    user_id = "os.getenv("USER_ID", "DEMO_USER")"
    
    query = "name bio family assets relationships"
    limit = 100
    
    print("=" * 80)
    print("🔍 OPTIMIZED SEMANTIC SEARCH TEST")
    print(f"User ID: {user_id}")
    print(f"Query: \"{query}\"")
    print(f"Limit: {limit}")
    print("=" * 80)
    
    start_time = time.time()
    
    try:
        # 1. Embedding
        print("\n1. Generating embedding...")
        t0 = time.time()
        query_vector = await embedding_service.get_embedding(query)
        t1 = time.time()
        print(f"   ✓ Done in {t1-t0:.2f}s (dim: {len(query_vector)})")
        
        # 2. Firestore Search
        print("\n2. Searching Firestore...")
        from google.cloud.firestore import FieldFilter
        from google.cloud.firestore_v1.vector import Vector
        from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
        
        collection_name = f"{env_config.firestore_collection_prefix}facts"
        
        t0 = time.time()
        vector_query = (
            db_client.collection(collection_name)
            .where(filter=FieldFilter("owner_id", "==", user_id))
            .where(filter=FieldFilter("is_current", "==", True))
            .find_nearest(
                vector_field="vector",
                query_vector=Vector(query_vector),
                distance_measure=DistanceMeasure.COSINE,
                limit=limit
            )
        )
        
        docs = await vector_query.get()
        t1 = time.time()
        print(f"   ✓ Done in {t1-t0:.2f}s (found {len(docs)} docs)")
        
        # 3. Processing & Filtering
        print("\n3. Filtering...")
        facts = []
        exclude_tags = ["test", "environment_isolation"]
        
        for doc in docs:
            data = doc.to_dict()
            tags = data.get("tags", [])
            
            # Filter
            if any(tag in tags for tag in exclude_tags):
                continue
                
            facts.append({
                "text": data.get("text", ""),
                "tags": tags,
                "similarity": 1.0 - data.get("__distance__", 1.0)
            })
            
        print(f"   ✓ {len(docs)} -> {len(facts)} facts (removed {len(docs)-len(facts)} noise)")
        
        total_time = time.time() - start_time
        print(f"\n⏱️  TOTAL TIME: {total_time:.2f}s")
        
        print("\n📊 Tag Distribution (Top 20):")
        print("-" * 40)
        all_tags = []
        for f in facts:
            all_tags.extend(f["tags"])
        
        for tag, count in Counter(all_tags).most_common(20):
            print(f"   {tag:<25} : {count}")
            
        print("\n📝 All Facts (Top 100):")
        print("-" * 80)
        for i, f in enumerate(facts, 1):  # Show all facts
            tags_str = ", ".join(f["tags"][:3])
            # Truncate text slightly longer for better visibility
            display_text = (f['text'][:90] + '...') if len(f['text']) > 90 else f['text']
            print(f"{i:3}. [{f['similarity']:.3f}] {display_text} [Tags: {tags_str}]")
            
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n" + "=" * 80)
    print("✅ Test completed!")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(test_semantic_search())

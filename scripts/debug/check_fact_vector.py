"""
Check if a specific fact has a vector embedding.
"""
import asyncio
import sys
import os

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from google.cloud import firestore
from google.cloud.firestore_v1.vector import Vector
from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig


async def check_fact_vector(fact_id: str, collection_name: str):
    """Check if fact has vector."""
    print(f"\n🔍 Checking fact: {fact_id}")
    print(f"📁 Collection: {collection_name}")
    print("=" * 80)
    
    # Setup
    config = load_settings()
    env_config = EnvironmentConfig()
    db_id = env_config.firestore_database_id
    
    print(f"🗄️  Database: {db_id}")
    print(f"🌍 Environment: {env_config}\n")
    
    db = firestore.AsyncClient(
        project=config["GOOGLE_CLOUD_PROJECT"],
        database=db_id
    )
    
    # Get document
    doc_ref = db.collection(collection_name).document(fact_id)
    doc = await doc_ref.get()
    
    if not doc.exists:
        print(f"❌ Fact NOT FOUND in Firestore!")
        return
    
    data = doc.to_dict()
    
    # Check fields
    print(f"\n📊 Fact data:")
    print(f"   ID: {fact_id}")
    print(f"   Account ID: {data.get('account_id', 'N/A')}")
    print(f"   Type: {data.get('type', 'N/A')}")
    print(f"   Tags: {data.get('tags', [])}")
    print(f"   is_current: {data.get('is_current', False)}")
    print(f"   Text: {data.get('text', 'N/A')[:100]}...")
    
    # Check vector
    has_vector = 'vector' in data
    print(f"\n🎯 Vector status:")
    if has_vector:
        vector = data['vector']
        if isinstance(vector, Vector):
            print(f"   ✅ HAS VECTOR (Firestore Vector type, dim={len(list(vector))})")
        elif isinstance(vector, list):
            print(f"   ✅ HAS VECTOR (list type, dim={len(vector)})")
        else:
            print(f"   ⚠️ HAS VECTOR but unknown type: {type(vector)}")
    else:
        print(f"   ❌ NO VECTOR FIELD!")
        print(f"   💡 This fact will NOT be found by vector search")
        print(f"   💡 Needs re-consolidation to generate embedding")
    
    # Check metadata
    if 'metadata' in data:
        metadata = data['metadata']
        print(f"\n📝 Metadata:")
        for key, value in metadata.items():
            print(f"   {key}: {value}")


async def main():
    fact_id = "bbb03e6c-649f-4cf1-b681-51156752ea20"
    collection = "development_domain_facts_v2"
    
    await check_fact_vector(fact_id, collection)


if __name__ == "__main__":
    asyncio.run(main())

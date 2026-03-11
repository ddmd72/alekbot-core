import asyncio
import os
import sys
from google.cloud import firestore

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), '../../'))

from src.config.settings import load_settings

async def check_owner_ids():
    print("🔍 Checking owner_ids in facts and observations...")
    
    # Load Config
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]
    
    if env_config.use_emulator:
        print(f"🏠 Using Firestore EMULATOR")
        db = firestore.AsyncClient(project="emulator-project")
    else:
        print(f"☁️ Using Firestore CLOUD in {env_config.env.value} mode")
        db = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
        
    prefix = env_config.firestore_collection_prefix
    facts_col = db.collection(f"{prefix}facts")
    obs_col = db.collection(f"{prefix}observations")
    
    # Check facts
    print("\n📝 Checking Facts...")
    facts = [d async for d in facts_col.limit(5).stream()]
    unique_owners = set()
    for doc in facts:
        data = doc.to_dict()
        owner_id = data.get('owner_id', 'NO_OWNER')
        unique_owners.add(owner_id)
        print(f"  Fact {doc.id[:16]}... | owner_id: {owner_id}")
    
    print(f"\n✅ Unique owner_ids in facts: {unique_owners}")
    
    # Check observations
    print("\n📦 Checking Observations...")
    obs = [d async for d in obs_col.limit(5).stream()]
    unique_obs_owners = set()
    for doc in obs:
        data = doc.to_dict()
        owner_id = data.get('owner_id', 'NO_OWNER')
        unique_obs_owners.add(owner_id)
        print(f"  Obs {doc.id[:16]}... | owner_id: {owner_id}")
    
    print(f"\n✅ Unique owner_ids in observations: {unique_obs_owners}")
    
    # Compare
    if unique_owners == unique_obs_owners:
        print("\n✅ MATCH: owner_ids are consistent!")
    else:
        print("\n❌ MISMATCH detected!")
        print(f"   Facts have: {unique_owners}")
        print(f"   Observations have: {unique_obs_owners}")

if __name__ == "__main__":
    asyncio.run(check_owner_ids())

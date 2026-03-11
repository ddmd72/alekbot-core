import asyncio
import os
import sys
from google.cloud import firestore

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), '../../'))

from src.config.settings import load_settings

async def check_archived_observations():
    print("🔍 Checking archived observations...")
    
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
    archive_col = db.collection(f"{prefix}observations_archive")
    
    # Get all archived observations
    print(f"\n📦 Checking {prefix}observations_archive...")
    archived = [d async for d in archive_col.stream()]
    
    print(f"Found {len(archived)} archived observations.")
    
    if len(archived) == 0:
        print("✅ Archive is empty - no migration needed.")
        return
    
    # Check for owner_id
    with_owner = 0
    without_owner = 0
    unique_owners = set()
    
    for doc in archived:
        data = doc.to_dict()
        owner_id = data.get('owner_id')
        if owner_id:
            with_owner += 1
            unique_owners.add(owner_id)
        else:
            without_owner += 1
    
    print(f"\n📊 Statistics:")
    print(f"  ✅ With owner_id: {with_owner}")
    print(f"  ❌ Without owner_id: {without_owner}")
    
    if unique_owners:
        print(f"\n👥 Unique owner_ids found: {unique_owners}")
    
    if without_owner > 0:
        print(f"\n⚠️ WARNING: {without_owner} archived observations need migration!")
    else:
        print(f"\n✅ All archived observations have owner_id!")

if __name__ == "__main__":
    asyncio.run(check_archived_observations())

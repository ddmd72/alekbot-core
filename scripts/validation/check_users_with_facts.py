import asyncio
import os
import sys
from google.cloud import firestore

# Add src to python path
sys.path.append(os.getcwd())

from src.config.settings import load_settings

async def list_users_with_facts():
    """List all users who have facts in the current environment."""
    
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]
    
    print(f"\n🌍 Environment: {env_config.env.value}")
    
    if env_config.use_emulator:
        print("🏠 Using Firestore EMULATOR")
        db = firestore.AsyncClient(project="emulator-project")
    else:
        print("☁️ Using Firestore CLOUD")
        db = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
        
    prefix = env_config.firestore_collection_prefix
    collection_name = f"{prefix}facts"
    print(f"📂 Scanning collection: {collection_name}\n")
    
    try:
        # Get all unique owner_ids
        # Note: In a large DB, this would be inefficient. For dev/debug it's fine.
        docs = await db.collection(collection_name).select(["owner_id"]).get()
        
        users = set()
        count = 0
        for doc in docs:
            data = doc.to_dict()
            if 'owner_id' in data:
                users.add(data['owner_id'])
            count += 1
            
        print(f"✅ Scanned {count} documents.")
        print(f"👥 Found {len(users)} unique users with facts:")
        
        for user_id in users:
            print(f"   - {user_id}")
            
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    asyncio.run(list_users_with_facts())

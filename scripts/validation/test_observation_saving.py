import asyncio
import os
import sys
import datetime
import uuid

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), '../../'))

from src.config.settings import load_settings
from src.adapters.firestore_repo import FirestoreFactRepository
from google.cloud import firestore

async def test_observation_saving():
    print("🧪 Testing Observation Saving...")
    
    # Load Config
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]
    
    if env_config.use_emulator:
        print(f"🏠 Using Firestore EMULATOR")
        db = firestore.AsyncClient(project="emulator-project")
    else:
        print(f"☁️ Using Firestore CLOUD in {env_config.env.value} mode")
        db = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
    
    # Initialize repository
    repo = FirestoreFactRepository(db, env_config)
    
    # Get all users
    prefix = env_config.firestore_collection_prefix
    users_col = db.collection(f"{prefix}users")
    users = [d async for d in users_col.stream()]
    
    print(f"\n👥 Found {len(users)} users:")
    for user_doc in users:
        user_data = user_doc.to_dict()
        print(f"  - {user_data['user_id']}: {user_data.get('display_name')} (Slack: {user_data.get('platform_identities', {}).get('slack')})")
    
    # Test saving for each user
    for user_doc in users:
        user_data = user_doc.to_dict()
        user_id = user_data['user_id']
        display_name = user_data.get('display_name', 'Unknown')
        
        print(f"\n🧪 Testing observation save for {display_name} ({user_id})...")
        
        test_obs = {
            "id": f"test_obs_{uuid.uuid4().hex[:8]}",
            "timestamp": datetime.datetime.now().isoformat(),
            "date": datetime.datetime.now().strftime("%Y-%m-%d"),
            "domain": "test",
            "content": f"Test observation for {display_name}",
            "confidence": "high",
            "source_context": "Debug test script"
        }
        
        try:
            await repo.add_observation(test_obs, owner_id=user_id)
            print(f"  ✅ Successfully saved test observation")
            
            # Verify it was saved
            obs_list = await repo.get_observations(owner_id=user_id, limit=1)
            if obs_list:
                print(f"  ✅ Verified: Found {len(obs_list)} observation(s) for this user")
                latest = obs_list[0]
                print(f"     Latest: {latest.get('content', '')[:50]}")
            else:
                print(f"  ❌ WARNING: No observations found after save!")
                
        except Exception as e:
            print(f"  ❌ ERROR saving observation: {e}")
            import traceback
            traceback.print_exc()
    
    print("\n✅ Test complete!")

if __name__ == "__main__":
    asyncio.run(test_observation_saving())

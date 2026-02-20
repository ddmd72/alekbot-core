import asyncio
import os
import sys
from google.cloud import firestore

# Add project root to path
sys.path.append(os.path.join(os.path.dirname(__file__), "../../"))

from src.config.environment import EnvironmentConfig
from src.config.settings import load_settings

async def check_dev_users():
    """Check all users in development environment."""
    # Force development environment
    os.environ["APP_ENV"] = "development"
    
    config = load_settings()
    env_config = EnvironmentConfig()
    project_id = config["GOOGLE_CLOUD_PROJECT"]
    db = firestore.AsyncClient(project=project_id)
    
    prefix = env_config.firestore_collection_prefix
    users_col = db.collection(f"{prefix}users")
    
    print(f"🔍 Checking users in {prefix}users (project: {project_id})...\n")
    
    count = 0
    async for doc in users_col.stream():
        count += 1
        data = doc.to_dict()
        config_data = data.get('config', {})
        
        print(f"{'='*60}")
        print(f"👤 User #{count}: {data.get('display_name', 'Unknown')}")
        print(f"   User ID: {data.get('user_id')}")
        print(f"   Slack ID: {data.get('platform_identities', {}).get('slack')}")
        print(f"   Tier: {data.get('tier')}")
        print(f"   Active: {data.get('is_active')}")
        print(f"\n   🤖 Bot Config:")
        print(f"      LLM Provider: {config_data.get('llm_provider')}")
        print(f"      Light Model: {config_data.get('light_model')}")
        print(f"      Full Model: {config_data.get('full_model')}")
        print(f"      Temperature: {config_data.get('temperature')}")
        print(f"      Tools: {config_data.get('tools_enabled')}")
        print()
    
    if count == 0:
        print("⚠️  No users found in collection")
    else:
        print(f"{'='*60}")
        print(f"✅ Total users: {count}")

if __name__ == "__main__":
    asyncio.run(check_dev_users())

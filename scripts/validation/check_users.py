import asyncio
import os
import sys
from google.cloud import firestore

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), '../../'))

from src.config.settings import load_settings

async def check_users():
    print("🔍 Checking users in Firestore...")
    
    # Load Config
    config = load_settings()
    # Force PROD for check
    project_id = config["GOOGLE_CLOUD_PROJECT"]
    db = firestore.AsyncClient(project=project_id)
    
    # Check PROD users
    print(f"\n☁️  Checking PRODUCTION users in project {project_id}...")
    users_col = db.collection("users") # Prod collection has no prefix usually, or 'users'
    # Wait, environment config defines prefix.
    # Let's check both 'users' and 'production_users' just in case, 
    # but usually it's just 'users' if prefix is empty for prod.
    
    # Actually, let's use the code logic
    # In environment.py: prefix is "" for production usually?
    # Let's check all users
    
    async for doc in users_col.stream():
        data = doc.to_dict()
        print(f"👤 User: {data.get('display_name', 'Unknown')}")
        print(f"   ID: {data.get('user_id')}")
        print(f"   Slack ID: {data.get('platform_identities', {}).get('slack')}")
        print(f"   Tier: {data.get('tier')}")
        print("---")

if __name__ == "__main__":
    asyncio.run(check_users())

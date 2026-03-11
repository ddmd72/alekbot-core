"""
Check consolidation queue for pending facts.
"""
import asyncio
import sys
import os

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from google.cloud import firestore
from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig


async def check_queue(account_id: str):
    """Check consolidation queue."""
    print(f"\n🔍 Checking consolidation queue for: {account_id}")
    print("=" * 80)
    
    config = load_settings()
    env_config = EnvironmentConfig()
    db = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
    collection = env_config.consolidation_queue_collection
    
    print(f"📂 Using collection: {collection}")
    print(f"🌍 Environment: {env_config}\n")
    
    # Query queue
    query = (db.collection(collection)
             .where(filter=firestore.FieldFilter("account_id", "==", account_id))
             .limit(50))
    
    docs = query.stream()
    
    items = []
    async for doc in docs:
        data = doc.to_dict()
        items.append({
            'id': doc.id,
            'status': data.get('status', 'N/A'),
            'fact_id': data.get('fact_id', 'N/A'),
            'created_at': data.get('created_at'),
            'message': data.get('message', '')[:100]
        })
    
    print(f"📊 Found {len(items)} items in queue:\n")
    
    for i, item in enumerate(items, 1):
        created = item['created_at']
        if hasattr(created, 'strftime'):
            created_str = created.strftime('%Y-%m-%d %H:%M:%S')
        else:
            created_str = str(created)
        
        print(f"[{i}] ID: {item['id']}")
        print(f"    Fact ID: {item['fact_id']}")
        print(f"    Status: {item['status']}")
        print(f"    Created: {created_str}")
        print(f"    Message: {item['message']}...")
        print()


async def main():
    user_id = os.getenv("USER_ID") or "DEMO_USER"
    account_id = f"account-{user_id}"
    await check_queue(account_id)


if __name__ == "__main__":
    asyncio.run(main())

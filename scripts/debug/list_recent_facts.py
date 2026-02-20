"""
List recent facts for a specific account_id.
"""
import asyncio
import sys
import os
from datetime import datetime

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from google.cloud import firestore
from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig


async def list_recent_facts(account_id: str, limit: int = 20):
    """List recent facts for account."""
    print(f"\n🔍 Recent facts for account: {account_id}")
    print("=" * 80)
    
    # Setup
    config = load_settings()
    env_config = EnvironmentConfig()
    db = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
    collection = env_config.domain_facts_collection
    
    print(f"📂 Using collection: {collection}")
    print(f"🌍 Environment: {env_config}")
    
    # Query recent facts - NO ORDER BY to avoid composite index requirement
    query = (db.collection(collection)
             .where(filter=firestore.FieldFilter("account_id", "==", account_id))
             .limit(limit))
    
    print("⚠️  NOTE: Results NOT sorted (composite index missing)")
    
    docs = query.stream()
    
    facts = []
    async for doc in docs:
        data = doc.to_dict()
        facts.append({
            'id': doc.id,
            'created_at': data.get('created_at'),
            'text': data.get('text', '')[:80],
            'tags': data.get('tags', []),
            'type': data.get('type', 'N/A'),
            'has_vector': 'vector' in data
        })
    
    print(f"\n📊 Found {len(facts)} facts:\n")
    
    for i, fact in enumerate(facts, 1):
        created = fact['created_at']
        if hasattr(created, 'strftime'):
            created_str = created.strftime('%Y-%m-%d %H:%M:%S')
        else:
            created_str = str(created)
        
        vector_status = "✅" if fact['has_vector'] else "❌"
        
        print(f"[{i}] {vector_status} {fact['id']}")
        print(f"    Created: {created_str}")
        print(f"    Type: {fact['type']}")
        print(f"    Tags: {fact['tags']}")
        print(f"    Text: {fact['text']}...")
        print()


async def main():
    user_id = os.getenv("USER_ID") or "DEMO_USER"
    account_id = f"account-{user_id}"
    await list_recent_facts(account_id, limit=20)


if __name__ == "__main__":
    asyncio.run(main())

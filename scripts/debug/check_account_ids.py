"""
Check account_id formats in facts collection.
Identifies inconsistencies between "account-xxx" vs "xxx" formats.
"""
import asyncio
import sys
import os
from collections import Counter

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from google.cloud import firestore
from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig


async def check_account_ids():
    """Check all account_id formats in collection."""
    print("\n🔍 Checking account_id formats in facts collection")
    print("=" * 80)
    
    config = load_settings()
    env_config = EnvironmentConfig()
    db_id = env_config.firestore_database_id
    collection = env_config.domain_facts_collection
    
    print(f"🗄️  Database: {db_id}")
    print(f"📂 Collection: {collection}")
    print(f"🌍 Environment: {env_config}\n")
    
    db = firestore.AsyncClient(
        project=config["GOOGLE_CLOUD_PROJECT"],
        database=db_id
    )
    
    # Get all facts
    query = db.collection(collection).limit(500)
    docs = query.stream()
    
    account_ids = []
    has_prefix_count = 0
    no_prefix_count = 0
    
    async for doc in docs:
        data = doc.to_dict()
        account_id = data.get('account_id', '')
        
        if account_id:
            account_ids.append(account_id)
            if account_id.startswith('account-'):
                has_prefix_count += 1
            else:
                no_prefix_count += 1
    
    print(f"📊 Analysis of {len(account_ids)} facts:\n")
    print(f"   ✅ With 'account-' prefix: {has_prefix_count}")
    print(f"   ❌ WITHOUT 'account-' prefix: {no_prefix_count}")
    
    # Count unique account IDs
    unique_ids = set(account_ids)
    print(f"\n🔑 Unique account_ids found: {len(unique_ids)}")
    
    # Show distribution
    id_counts = Counter(account_ids)
    print(f"\n📈 Top account_ids:")
    for account_id, count in id_counts.most_common(10):
        prefix_marker = "✅" if account_id.startswith('account-') else "❌"
        print(f"   {prefix_marker} {account_id}: {count} facts")
    
    # Check specific user's facts
    print(f"\n🎯 Checking specific user facts:")
    target_user = os.getenv("USER_ID", "DEMO_USER")
    
    # Check both formats
    for prefix in ["", "account-"]:
        test_id = f"{prefix}{target_user}"
        count = id_counts.get(test_id, 0)
        marker = "✅" if count > 0 else "❌"
        print(f"   {marker} {test_id}: {count} facts")


async def main():
    await check_account_ids()


if __name__ == "__main__":
    asyncio.run(main())

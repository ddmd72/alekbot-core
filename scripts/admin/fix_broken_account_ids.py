"""
Fix broken account_ids in facts collection.
Updates 8 facts with wrong format (without "account-" prefix).
"""
import asyncio
import sys
import os

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from google.cloud import firestore
from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig


async def fix_account_ids(dry_run: bool = True):
    """Fix account_ids in broken facts."""
    print("\n🔧 Fixing broken account_ids in facts")
    print("=" * 80)
    
    config = load_settings()
    env_config = EnvironmentConfig()
    db_id = env_config.firestore_database_id
    collection = env_config.domain_facts_collection
    
    print(f"🗄️  Database: {db_id}")
    print(f"📂 Collection: {collection}")
    print(f"🌍 Environment: {env_config}")
    print(f"🔍 Mode: {'DRY RUN' if dry_run else 'LIVE UPDATE'}\n")
    
    if not dry_run and env_config.is_production:
        print("🔴 PRODUCTION MODE - Updates will be LIVE!")
        response = input("Type 'yes' to continue: ")
        if response.lower() != 'yes':
            print("Aborted.")
            return
    
    db = firestore.AsyncClient(
        project=config["GOOGLE_CLOUD_PROJECT"],
        database=db_id
    )
    
    # Find broken facts (without "account-" prefix)
    # We need to query all facts and filter locally since Firestore
    # doesn't support "NOT starts with" queries
    
    query = db.collection(collection).limit(1000)
    docs = query.stream()
    
    broken_facts = []
    async for doc in docs:
        data = doc.to_dict()
        account_id = data.get('account_id', '')
        
        if account_id and not account_id.startswith('account-'):
            broken_facts.append({
                'id': doc.id,
                'account_id': account_id,
                'new_account_id': f"account-{account_id}",
                'text': data.get('text', '')[:80]
            })
    
    print(f"📊 Found {len(broken_facts)} broken facts:\n")
    
    for i, fact in enumerate(broken_facts, 1):
        print(f"[{i}] ID: {fact['id']}")
        print(f"    Old: {fact['account_id']}")
        print(f"    New: {fact['new_account_id']}")
        print(f"    Text: {fact['text']}...")
        print()
    
    if len(broken_facts) == 0:
        print("✅ No broken facts found!")
        return
    
    if dry_run:
        print("\n🔍 DRY RUN - No changes made")
        print("Run with --live to apply fixes")
        return
    
    # Apply fixes
    print(f"\n🔄 Updating {len(broken_facts)} facts...")
    
    batch = db.batch()
    for fact in broken_facts:
        doc_ref = db.collection(collection).document(fact['id'])
        batch.update(doc_ref, {'account_id': fact['new_account_id']})
    
    await batch.commit()
    
    print(f"✅ Updated {len(broken_facts)} facts successfully!")


async def main():
    import sys
    
    dry_run = '--live' not in sys.argv
    await fix_account_ids(dry_run=dry_run)


if __name__ == "__main__":
    asyncio.run(main())

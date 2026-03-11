import asyncio
import logging
from google.cloud import firestore
from src.config.environment import EnvironmentConfig
from src.adapters.firestore_repo import FirestoreFactRepository

logger = logging.getLogger(__name__)

async def warmup_cache():
    """
    Triggers biographical context cache refresh for all users who have facts.
    """
    config = EnvironmentConfig()
    db = firestore.AsyncClient()
    repo = FirestoreFactRepository(db, config)
    await repo.initialize()
    
    prefix = config.firestore_collection_prefix
    facts_col = f"{prefix}facts"
    
    print(f"🔍 Finding unique owners in {facts_col}...")
    
    # Get unique owner_ids from facts
    # Note: Firestore doesn't have 'distinct', so we iterate or use a known users collection
    # We'll use the facts collection to find active users
    owners = set()
    async for doc in db.collection(facts_col).select(["owner_id"]).stream():
        owners.add(doc.get("owner_id"))
    
    print(f"👤 Found {len(owners)} unique owners. Starting cache warmup...")
    
    for owner_id in owners:
        print(f"⏳ Refreshing cache for {owner_id[:8]}...")
        await repo.refresh_biographical_context_cache(owner_id)
    
    print("✅ Cache warmup complete.")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(warmup_cache())

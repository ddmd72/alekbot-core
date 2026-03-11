import asyncio
import logging
from google.cloud import firestore
from src.config.environment import EnvironmentConfig
from src.adapters.firestore_session_store import FirestoreSessionStore

logger = logging.getLogger(__name__)

async def migrate_sessions():
    """
    Migration script to add new lifecycle fields to existing sessions.
    """
    config = EnvironmentConfig()
    db = firestore.AsyncClient()
    
    prefix = config.firestore_collection_prefix
    collection_name = f"{prefix}sessions"
    collection = db.collection(collection_name)
    
    logger.info(f"🚀 Starting migration for collection: {collection_name}")
    
    docs = collection.stream()
    count = 0
    
    async for doc in docs:
        data = doc.to_dict()
        updates = {}
        
        if "message_count" not in data:
            # Estimate count from messages array length
            messages = data.get("messages", [])
            updates["message_count"] = len(messages)
            
        if "last_consolidation_at" not in data:
            updates["last_consolidation_at"] = None
            
        if updates:
            await collection.document(doc.id).update(updates)
            count += 1
            if count % 10 == 0:
                logger.info(f"✅ Migrated {count} sessions...")

    logger.info(f"🎉 Migration complete. Updated {count} sessions.")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(migrate_sessions())

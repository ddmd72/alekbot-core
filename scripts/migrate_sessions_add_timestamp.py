import asyncio
import time
import os
import sys
from google.cloud import firestore

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config.settings import load_settings

async def migrate_sessions():
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]
    
    db = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
    collection_name = f"{env_config.firestore_collection_prefix}sessions"
    
    print(f"🔄 Migrating sessions in: {collection_name}")
    
    # 1. BACKUP
    backup_collection = f"{collection_name}_backup_{int(time.time())}"
    print(f"📦 Creating backup: {backup_collection}")
    
    sessions = db.collection(collection_name).stream()
    backup_count = 0
    async for session in sessions:
        await db.collection(backup_collection).document(session.id).set(session.to_dict())
        backup_count += 1
    
    print(f"✅ Backup complete: {backup_count} sessions")
    
    # 2. MIGRATION
    print("🔨 Adding created_at to messages...")
    sessions = db.collection(collection_name).stream()
    
    migrated_count = 0
    async for session_doc in sessions:
        data = session_doc.to_dict()
        history = data.get("history", [])
        
        updated = False
        last_activity = data.get("last_activity", time.time())
        created_at_session = data.get("created_at", last_activity)
        
        for msg in history:
            if "created_at" not in msg:
                # Use session created_at as fallback
                msg["created_at"] = created_at_session
                updated = True
        
        if updated:
            await db.collection(collection_name).document(session_doc.id).update({"history": history})
            migrated_count += 1
            if migrated_count % 10 == 0:
                print(f"   Processed {migrated_count} sessions...")
    
    print(f"✅ Migration complete: {migrated_count} sessions updated")
    print(f"📄 Backup saved as: {backup_collection}")

if __name__ == "__main__":
    asyncio.run(migrate_sessions())

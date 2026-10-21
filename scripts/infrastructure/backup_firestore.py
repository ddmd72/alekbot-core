import asyncio
import sys
import time
import argparse
from google.cloud import firestore
from src.config.environment import EnvironmentConfig

async def backup_collections(collections_list):
    config = EnvironmentConfig()
    db = firestore.AsyncClient(database=config.firestore_database_id)

    # Require explicit confirmation when targeting production
    prefix = config.firestore_collection_prefix
    if config.is_production:
        answer = input(f"⚠️  PRODUCTION backup (prefix='{prefix}'). Type 'YES' to continue: ")
        if answer.strip() != "YES":
            print("Aborted.")
            sys.exit(1)
    timestamp = int(time.time())
    
    for source_col_name in collections_list:
        backup_col_name = f"{source_col_name}_backup_{timestamp}"
        
        print(f"📦 Backing up {source_col_name} to {backup_col_name}...")
        
        source_col = db.collection(source_col_name)
        backup_col = db.collection(backup_col_name)
        
        count = 0
        async for doc in source_col.stream():
            await backup_col.document(doc.id).set(doc.to_dict())
            count += 1
            if count % 50 == 0:
                print(f"  - Copied {count} documents...")
        
        print(f"✅ Backup complete for {source_col_name}. Total: {count} docs.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--collections", default=None, help="Comma-separated FULL collection names (default: live facts + sessions)")
    args = parser.parse_args()

    env = EnvironmentConfig()
    collections = args.collections.split(",") if args.collections else [env.domain_facts_collection, env.sessions_collection]
    asyncio.run(backup_collections(collections))

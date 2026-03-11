import asyncio
import logging
import argparse
import sys
import os
from typing import List

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from google.cloud import firestore
from src.config.settings import load_settings

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

LEGACY_COLLECTIONS = [
    "development_users",
    "development_users_oauth",
    "development_accounts_oauth",
    "development_facts",
    "development_facts_oauth",
    "development_user_context_oauth",
    "development_observations_archive",
    "development_prompt_components",
    "dev_prompt_system_tokens",
    "dev_prompt_user_tokens",
    "dev_prompt_blueprints",
    "dev_prompt_blueprints_v3",
    "dev_prompt_agent_profiles",
    "dev_prompt_agent_profile_user_overrides"
]

class FirestoreCleaner:
    def __init__(self, db: firestore.AsyncClient, batch_size: int = 400):
        self.db = db
        self.batch_size = batch_size

    async def delete_collection(self, collection_name: str):
        """Recursively delete collection documents in batches."""
        logger.info(f"🗑️  Deleting collection: {collection_name}...")
        col_ref = self.db.collection(collection_name)
        
        deleted_count = 0
        while True:
            # Get a batch of documents
            docs = col_ref.limit(self.batch_size).stream()
            batch = self.db.batch()
            count = 0
            
            async for doc in docs:
                batch.delete(doc.reference)
                count += 1
            
            if count == 0:
                break
                
            await batch.commit()
            deleted_count += count
            logger.info(f"   Deleted {count} documents (Total: {deleted_count})...")
            
        logger.info(f"✅ Collection {collection_name} cleared ({deleted_count} docs).")

async def main():
    parser = argparse.ArgumentParser(description="Delete legacy Firestore collections")
    parser.add_argument("--confirm", action="store_true", help="Confirm deletion")
    args = parser.parse_args()

    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]

    if not args.confirm:
        logger.warning("⚠️  Run with --confirm to actually delete data!")
        logger.info("Collections to delete:")
        for col in LEGACY_COLLECTIONS:
            logger.info(f" - {col}")
        return

    logger.info("="*60)
    logger.info(f"🔥 Firestore Cleaner Tool")
    logger.info("="*60)

    # Initialize Firestore
    if env_config.use_emulator:
        db = firestore.AsyncClient(project="emulator-project")
    else:
        db = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])

    cleaner = FirestoreCleaner(db)

    try:
        for col in LEGACY_COLLECTIONS:
            await cleaner.delete_collection(col)
            
        logger.info("\n✨ All legacy collections deleted successfully.")
        
    except Exception as e:
        logger.error(f"❌ Deletion failed: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())

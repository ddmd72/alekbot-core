import asyncio
import logging
import argparse
import sys
import os
from typing import Dict, Any, List

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from google.cloud import firestore
from src.config.settings import load_settings

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Mapping: Development (Source) -> Production (Target)
CLONE_MAPPING = {
    # Domain
    "development_domain_users_v2": "domain_users_v2",
    "development_domain_accounts_v2": "domain_accounts_v2",
    "development_domain_facts_v2": "domain_facts_v2",
    
    # Prompt System v3
    "development_domain_prompt_tokens_v3_system": "domain_prompt_tokens_v3_system",
    "development_domain_prompt_tokens_v3_user": "domain_prompt_tokens_v3_user",
    "development_domain_prompt_blueprints_v3": "domain_prompt_blueprints_v3",
    "development_domain_prompt_profiles_v3": "domain_prompt_profiles_v3",
    "development_domain_prompt_overrides_v3": "domain_prompt_overrides_v3",
    
    # Infrastructure
    "development_sessions": "sessions",
    "development_consolidation_queue": "consolidation_queue",
    "development_event_dedup": "event_dedup",
    "development_user_context": "user_context"
}

class FirestoreCloner:
    def __init__(self, db: firestore.AsyncClient, batch_size: int = 400):
        self.db = db
        self.batch_size = batch_size

    async def clear_collection(self, collection_name: str):
        """Delete all documents in a collection."""
        logger.info(f"🗑️  Clearing target collection: {collection_name}...")
        col_ref = self.db.collection(collection_name)
        
        while True:
            docs = col_ref.limit(self.batch_size).stream()
            batch = self.db.batch()
            count = 0
            
            async for doc in docs:
                batch.delete(doc.reference)
                count += 1
            
            if count == 0:
                break
            
            await batch.commit()
            logger.info(f"   Deleted {count} documents...")

    async def clone_collection(self, source: str, target: str):
        """Copy all documents from source to target."""
        logger.info(f"📦 Cloning {source} -> {target}...")
        
        # 1. Clear target first
        await self.clear_collection(target)
        
        # 2. Copy data
        source_col = self.db.collection(source)
        target_col = self.db.collection(target)
        
        docs = source_col.stream()
        batch = self.db.batch()
        count = 0
        total = 0
        
        async for doc in docs:
            data = doc.to_dict()
            batch.set(target_col.document(doc.id), data)
            count += 1
            total += 1
            
            if count >= self.batch_size:
                await batch.commit()
                batch = self.db.batch()
                logger.info(f"   Copied {total} documents...")
                count = 0
        
        if count > 0:
            await batch.commit()
            
        logger.info(f"✅ Cloned {total} documents from {source} to {target}")

async def main():
    parser = argparse.ArgumentParser(description="Clone Development Firestore to Production")
    parser.add_argument("--confirm", action="store_true", help="Confirm overwrite of Production data")
    args = parser.parse_args()

    if not args.confirm:
        logger.warning("⚠️  THIS WILL OVERWRITE PRODUCTION DATA!")
        logger.warning("   Run with --confirm to execute.")
        return

    config = load_settings()
    db = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
    
    cloner = FirestoreCloner(db)
    
    logger.info("="*60)
    logger.info("🔄 Firestore Clone Tool (Dev -> Prod)")
    logger.info("="*60)
    
    try:
        for source, target in CLONE_MAPPING.items():
            await cloner.clone_collection(source, target)
            
        logger.info("\n✨ Clone completed successfully.")
        
    except Exception as e:
        logger.error(f"❌ Clone failed: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())

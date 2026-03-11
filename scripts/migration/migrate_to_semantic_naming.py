import asyncio
import logging
import argparse
import sys
import os
from datetime import datetime
from typing import List, Dict, Any, Optional

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from google.cloud import firestore
from src.config.settings import load_settings
from src.config.environment import EnvironmentConfig, Environment

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Migration Configuration ---

# Mapping OLD_NAME -> NEW_NAME
# Based on ADR-006: Semantic Separation
MIGRATION_MAP_DEV = {
    # Domain: Identity & Billing
    "development_users_oauth": "development_domain_users_v2",
    "development_accounts_oauth": "development_domain_accounts_v2",
    
    # Domain: Memory
    "development_facts_oauth": "development_domain_facts_v2",
    
    # Domain: Prompt System v3 (Hardcoded dev_ prefix -> Semantic)
    "dev_prompt_system_tokens": "development_domain_prompt_tokens_v3_system",
    "dev_prompt_user_tokens": "development_domain_prompt_tokens_v3_user",
    "dev_prompt_blueprints": "development_domain_prompt_blueprints_v3",
    "dev_prompt_agent_profiles": "development_domain_prompt_profiles_v3",
    "dev_prompt_user_token_overrides": "development_domain_prompt_overrides_v3",
    
    # Infrastructure: No change (Self-to-Self mapping for verification)
    "development_sessions": "development_sessions",
    "development_consolidation_queue": "development_consolidation_queue",
    "development_event_dedup": "development_event_dedup",
    "development_user_context": "development_user_context",
}

class MigrationManager:
    def __init__(self, db: firestore.AsyncClient, dry_run: bool = True):
        self.db = db
        self.dry_run = dry_run
        self.batch_size = 500  # Firestore batch limit

    async def migrate_collection(self, old_name: str, new_name: str):
        """
        Copy all documents from old collection to new collection.
        Does NOT delete old data.
        """
        if old_name == new_name:
            logger.info(f"⏭️ Skipping {old_name} (Same name)")
            return

        logger.info(f"🚀 Migrating {old_name} -> {new_name}...")
        
        old_col = self.db.collection(old_name)
        new_col = self.db.collection(new_name)
        
        # 1. Count documents (approximation)
        docs = old_col.stream()
        count = 0
        batch = self.db.batch()
        
        async for doc in docs:
            count += 1
            data = doc.to_dict()
            
            # Special handling for vectors (if needed)
            # Firestore client handles Vector objects automatically
            
            if not self.dry_run:
                batch.set(new_col.document(doc.id), data)
                
                if count % self.batch_size == 0:
                    await batch.commit()
                    batch = self.db.batch()
                    logger.info(f"   Processed {count} documents...")
        
        if not self.dry_run and count > 0:
            await batch.commit()  # Commit remaining
            
        logger.info(f"✅ {old_name}: Found {count} documents.")
        
        if self.dry_run:
            logger.info("   [DRY RUN] No data written.")
        else:
            logger.info(f"   Written to {new_name}.")

    async def verify_counts(self, mapping: Dict[str, str]):
        """Verify doc counts match between old and new."""
        logger.info("\n📊 Verifying Migration...")
        
        for old_name, new_name in mapping.items():
            if old_name == new_name:
                continue
                
            old_count = await self._count_docs(old_name)
            new_count = await self._count_docs(new_name)
            
            status = "✅ MATCH" if old_count == new_count else "❌ MISMATCH"
            logger.info(f"{status}: {old_name}({old_count}) -> {new_name}({new_count})")

    async def _count_docs(self, collection_name: str) -> int:
        col = self.db.collection(collection_name)
        # Count aggregation query is cheaper/faster
        count_query = col.count()
        results = await count_query.get()
        return results[0][0].value

async def main():
    parser = argparse.ArgumentParser(description="Migrate Firestore collections to Semantic Naming (ADR-006)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate migration without writing")
    parser.add_argument("--env", type=str, default="development", choices=["development"], help="Environment to migrate")
    args = parser.parse_args()

    # 1. Load Config
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]
    
    if args.env != "development":
        logger.error("❌ Only DEVELOPMENT migration is currently supported.")
        sys.exit(1)
        
    if not env_config.is_development:
        logger.error("❌ APP_ENV must be 'development'")
        sys.exit(1)

    logger.info("="*60)
    logger.info(f"🛡️  Firestore Migration Tool (ADR-006)")
    logger.info(f"🌍 Environment: {args.env}")
    logger.info(f"🧪 Dry Run: {args.dry_run}")
    logger.info("="*60)

    # 2. Initialize Firestore
    if env_config.use_emulator:
        logger.info(f"🏠 Using Emulator: {env_config.get_emulator_host()}")
        db = firestore.AsyncClient(project="emulator-project")
    else:
        logger.info(f"☁️  Using Cloud Firestore: {config['GOOGLE_CLOUD_PROJECT']}")
        db = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])

    manager = MigrationManager(db, dry_run=args.dry_run)

    # 3. Execute Migration
    try:
        mapping = MIGRATION_MAP_DEV
        
        for old_col, new_col in mapping.items():
            await manager.migrate_collection(old_col, new_col)
            
        if not args.dry_run:
            await manager.verify_counts(mapping)
            
        logger.info("\n✨ Migration completed successfully.")
        
    except Exception as e:
        logger.error(f"❌ Migration failed: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())

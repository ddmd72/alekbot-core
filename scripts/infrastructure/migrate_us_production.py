#!/usr/bin/env python3
"""
Migrate Development collections to Production in us-production database.
Copies all data from development_* → production (no prefix).
"""
import asyncio
import logging
import argparse
import sys
from typing import Dict, Any

import os

from google.cloud import firestore

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

PROJECT_ID = os.environ["GOOGLE_CLOUD_PROJECT"]
DATABASE_ID = os.environ.get("FIRESTORE_DATABASE", "us-production")
BATCH_SIZE = 400

# Migration mapping: DEV → PROD
MIGRATION_MAPPING = {
    # Domain
    "development_domain_users_v2": "domain_users_v2",
    "development_domain_accounts_v2": "domain_accounts_v2",
    "development_domain_facts_v2": "domain_facts_v2",
    "development_domain_invite_codes_v1": "domain_invite_codes_v1",
    "development_domain_whitelist_v1": "domain_whitelist_v1",
    
    # Prompt System v3
    "development_domain_prompt_tokens_v3_system": "domain_prompt_tokens_v3_system",
    "development_domain_prompt_tokens_v3_user": "domain_prompt_tokens_v3_user",
    "development_domain_prompt_blueprints_v3": "domain_prompt_blueprints_v3",
    "development_domain_prompt_profiles_v3": "domain_prompt_profiles_v3",
    
    # Infrastructure
    "development_sessions": "sessions",
    "development_consolidation_queue": "consolidation_queue",
    "development_event_dedup": "event_dedup",
    "development_user_context": "user_context",
}


class FirestoreMigrator:
    def __init__(self, db: firestore.AsyncClient, batch_size: int = BATCH_SIZE):
        self.db = db
        self.batch_size = batch_size
        self.stats = {
            "collections_migrated": 0,
            "total_docs_copied": 0,
            "total_docs_deleted": 0,
        }

    async def clear_collection(self, collection_name: str) -> int:
        """Delete all documents in a collection. Returns count deleted."""
        logger.info(f"🗑️  Clearing target collection: {collection_name}...")
        col_ref = self.db.collection(collection_name)
        
        total_deleted = 0
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
            total_deleted += count
            logger.info(f"   Deleted {count} documents (total: {total_deleted})...")
        
        return total_deleted

    async def clone_collection(self, source: str, target: str, dry_run: bool = False):
        """Copy all documents from source to target."""
        logger.info(f"{'[DRY RUN] ' if dry_run else ''}📦 Migrating {source} → {target}...")
        
        # 1. Count source documents first
        source_col = self.db.collection(source)
        count_query = source_col.limit(1).stream()
        has_docs = False
        async for _ in count_query:
            has_docs = True
            break
        
        if not has_docs:
            logger.warning(f"⚠️  Source collection {source} is EMPTY - skipping")
            return
        
        if dry_run:
            logger.info(f"   [DRY RUN] Would clear {target} and copy from {source}")
            return
        
        # 2. Clear target first
        deleted = await self.clear_collection(target)
        self.stats["total_docs_deleted"] += deleted
        
        # 3. Copy data
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
                logger.info(f"   ✅ Copied {total} documents...")
                count = 0
        
        # Commit remaining
        if count > 0:
            await batch.commit()
            
        logger.info(f"✅ Migrated {source} → {target} ({total} documents)")
        self.stats["collections_migrated"] += 1
        self.stats["total_docs_copied"] += total


async def main():
    parser = argparse.ArgumentParser(
        description="Migrate Development → Production (us-production database)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes"
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Confirm migration (REQUIRED for live run)"
    )
    args = parser.parse_args()

    if not args.dry_run and not args.confirm:
        logger.error("❌ LIVE migration requires --confirm flag")
        logger.error("   Run with --dry-run first to preview changes")
        sys.exit(1)

    # Connect to Firestore
    db = firestore.AsyncClient(project=PROJECT_ID, database=DATABASE_ID)
    migrator = FirestoreMigrator(db)
    
    logger.info("="*80)
    logger.info("🚀 US-PRODUCTION MIGRATION (DEV → PROD)")
    logger.info("="*80)
    logger.info(f"📦 Project: {PROJECT_ID}")
    logger.info(f"💾 Database: {DATABASE_ID}")
    logger.info(f"📊 Collections: {len(MIGRATION_MAPPING)}")
    logger.info(f"🔧 Mode: {'DRY RUN' if args.dry_run else 'LIVE MIGRATION'}")
    logger.info("="*80)
    logger.info("")
    
    try:
        for source, target in MIGRATION_MAPPING.items():
            await migrator.clone_collection(source, target, dry_run=args.dry_run)
            logger.info("")  # Empty line between collections
        
        logger.info("="*80)
        logger.info("✨ MIGRATION SUMMARY")
        logger.info("="*80)
        logger.info(f"Collections migrated: {migrator.stats['collections_migrated']}/{len(MIGRATION_MAPPING)}")
        logger.info(f"Documents copied: {migrator.stats['total_docs_copied']}")
        logger.info(f"Documents deleted: {migrator.stats['total_docs_deleted']}")
        logger.info("="*80)
        
        if not args.dry_run:
            logger.info("")
            logger.info("✅ MIGRATION COMPLETE!")
            logger.info("")
            logger.info("⚠️  IMPORTANT: Vector indexes will re-index automatically.")
            logger.info("   Check index status with:")
            logger.info("   gcloud firestore indexes list --database=us-production")
        
    except Exception as e:
        logger.error(f"❌ Migration failed: {e}", exc_info=True)
        sys.exit(1)
    finally:
        # Close database connection (AsyncClient.close() returns None)
        close_result = db.close()
        if close_result is not None:
            await close_result


if __name__ == "__main__":
    asyncio.run(main())

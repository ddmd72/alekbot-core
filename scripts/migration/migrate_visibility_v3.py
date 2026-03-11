#!/usr/bin/env python3
"""
Visibility Migration Script (OAuth Multi-Tenant V3)
====================================================
Migrates legacy 'private' visibility values to 'account_shared' (default).

Old schema: visibility='private'
New schema: visibility='account_shared' | 'user_private'

Usage:
    python scripts/migration/migrate_visibility_v3.py --env development
    python scripts/migration/migrate_visibility_v3.py --env development --dry-run
"""

import asyncio
import argparse
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from google.cloud import firestore
from src.config.settings import load_settings

async def migrate_visibility(env: str, dry_run: bool = False):
    """
    Migrate visibility field from 'private' to 'account_shared'.
    
    Args:
        env: Environment (development/production)
        dry_run: If True, only report what would be changed
    """
    print(f"\n{'='*70}")
    print(f"🔄 VISIBILITY MIGRATION (OAuth Multi-Tenant V3)")
    print(f"{'='*70}")
    print(f"Environment: {env}")
    print(f"Mode: {'DRY RUN (no changes)' if dry_run else 'LIVE (will modify data)'}")
    print(f"{'='*70}\n")
    
    # Load config
    config = load_settings()
    project = config["GOOGLE_CLOUD_PROJECT"]
    
    # Create Firestore client
    db = firestore.AsyncClient(project=project)
    
    # Determine collection name
    if env == "development":
        collection_name = "development_facts_oauth"
    else:
        collection_name = "facts_oauth"
    
    print(f"📂 Collection: {collection_name}\n")
    
    # Query facts with visibility='private'
    print("🔍 Searching for facts with visibility='private'...")
    facts_col = db.collection(collection_name)
    query = facts_col.where("visibility", "==", "private")
    docs = await query.get()
    
    print(f"✅ Found {len(docs)} facts to migrate\n")
    
    if len(docs) == 0:
        print("🎉 No migration needed!")
        return
    
    # Show sample
    print("📋 Sample facts (first 5):")
    for i, doc in enumerate(docs[:5]):
        data = doc.to_dict()
        text = data.get("text", "NO TEXT")[:60]
        account_id = data.get("account_id", data.get("owner_id", "UNKNOWN"))
        print(f"   {i+1}. {doc.id[:20]}... | {account_id[:30]}... | {text}...")
    print()
    
    if dry_run:
        print("🔍 DRY RUN: Would migrate these facts to visibility='account_shared'")
        print(f"Total: {len(docs)} facts")
        return
    
    # Confirm before proceeding
    if env == "production":
        print("⚠️  WARNING: This will modify PRODUCTION data!")
        confirm = input("Type 'YES' to proceed: ")
        if confirm != "YES":
            print("❌ Migration cancelled")
            return
    
    # Perform migration
    print(f"\n🚀 Migrating {len(docs)} facts...")
    batch = db.batch()
    batch_count = 0
    total_migrated = 0
    
    for doc in docs:
        batch.update(doc.reference, {"visibility": "account_shared"})
        batch_count += 1
        
        # Commit in batches of 500 (Firestore limit)
        if batch_count >= 500:
            await batch.commit()
            total_migrated += batch_count
            print(f"   ✓ Migrated {total_migrated}/{len(docs)} facts...")
            batch = db.batch()
            batch_count = 0
    
    # Commit remaining
    if batch_count > 0:
        await batch.commit()
        total_migrated += batch_count
    
    print(f"\n{'='*70}")
    print(f"✅ MIGRATION COMPLETE")
    print(f"{'='*70}")
    print(f"Total migrated: {total_migrated} facts")
    print(f"'private' → 'account_shared'")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migrate visibility field for OAuth Multi-Tenant V3"
    )
    parser.add_argument(
        "--env",
        required=True,
        choices=["development", "production"],
        help="Environment to migrate"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry run mode (no changes)"
    )
    
    args = parser.parse_args()
    
    try:
        asyncio.run(migrate_visibility(args.env, args.dry_run))
    except Exception as e:
        print(f"\n❌ Migration failed: {e}")
        sys.exit(1)

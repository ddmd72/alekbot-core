#!/usr/bin/env python3
"""
Account ID Prefix Migration
============================
Adds 'account-' prefix to all account_id fields that don't have it.

OAuth Multi-Tenant V3: account_id should always have 'account-' prefix for type safety.

Usage:
    python scripts/migration/migrate_account_id_prefix.py --env development --dry-run
    python scripts/migration/migrate_account_id_prefix.py --env development
"""

import asyncio
import argparse
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from google.cloud import firestore
from src.config.settings import load_settings

async def migrate_account_prefix(env: str, dry_run: bool = False):
    """
    Add 'account-' prefix to account_id fields.
    
    Migrates:
    - users_oauth collection: account_id field
    - facts_oauth collection: account_id field (if exists)
    - observations_oauth collection: account_id field (if exists)
    
    Args:
        env: Environment (development/production)
        dry_run: If True, only report what would be changed
    """
    print(f"\n{'='*70}")
    print(f"🔄 ACCOUNT ID PREFIX MIGRATION (OAuth Multi-Tenant V3)")
    print(f"{'='*70}")
    print(f"Environment: {env}")
    print(f"Mode: {'DRY RUN (no changes)' if dry_run else 'LIVE (will modify data)'}")
    print(f"{'='*70}\n")
    
    # Load config
    config = load_settings()
    project = config["GOOGLE_CLOUD_PROJECT"]
    
    # Create Firestore client
    db = firestore.AsyncClient(project=project)
    
    # Determine collection names
    if env == "development":
        users_col = "development_users_oauth"
        facts_col = "development_facts_oauth"
        obs_col = "development_observations_oauth"
        accounts_col = "development_accounts_oauth"
    else:
        users_col = "users_oauth"
        facts_col = "facts_oauth"
        obs_col = "observations_oauth"
        accounts_col = "accounts_oauth"
    
    total_migrated = 0
    
    # ========================================================================
    # 1. Migrate BillingAccounts (accounts_oauth)
    # ========================================================================
    print(f"📂 1. Migrating {accounts_col}...")
    accounts = db.collection(accounts_col)
    
    # Find accounts WITHOUT prefix
    query = accounts.where(filter=firestore.FieldFilter("account_id", "!=", None))
    docs = await query.get()
    
    accounts_to_migrate = []
    for doc in docs:
        account_id = doc.get("account_id")
        if account_id and not account_id.startswith("account-"):
            accounts_to_migrate.append((doc.id, account_id))
    
    print(f"   Found {len(accounts_to_migrate)} accounts without prefix")
    
    if accounts_to_migrate:
        print("\n   Sample accounts:")
        for doc_id, account_id in accounts_to_migrate[:3]:
            print(f"      {doc_id}: '{account_id}' → 'account-{account_id}'")
        
        if not dry_run:
            batch = db.batch()
            batch_count = 0
            
            for doc_id, old_id in accounts_to_migrate:
                new_id = f"account-{old_id}"
                doc_ref = accounts.document(doc_id)
                batch.update(doc_ref, {"account_id": new_id})
                batch_count += 1
                
                if batch_count >= 500:
                    await batch.commit()
                    total_migrated += batch_count
                    print(f"      ✓ Migrated {total_migrated} accounts...")
                    batch = db.batch()
                    batch_count = 0
            
            if batch_count > 0:
                await batch.commit()
                total_migrated += batch_count
            
            print(f"   ✅ Migrated {len(accounts_to_migrate)} accounts\n")
    else:
        print(f"   ✅ All accounts already have prefix\n")
    
    # ========================================================================
    # 2. Migrate UserProfiles (users_oauth)
    # ========================================================================
    print(f"📂 2. Migrating {users_col}...")
    users = db.collection(users_col)
    
    # Find users with account_id WITHOUT prefix
    query = users.where(filter=firestore.FieldFilter("account_id", "!=", None))
    docs = await query.get()
    
    users_to_migrate = []
    for doc in docs:
        account_id = doc.get("account_id")
        if account_id and not account_id.startswith("account-"):
            users_to_migrate.append((doc.id, account_id))
    
    print(f"   Found {len(users_to_migrate)} users without prefix")
    
    if users_to_migrate:
        print("\n   Sample users:")
        for doc_id, account_id in users_to_migrate[:3]:
            print(f"      {doc_id}: account_id '{account_id}' → 'account-{account_id}'")
        
        if not dry_run:
            batch = db.batch()
            batch_count = 0
            
            for doc_id, old_id in users_to_migrate:
                new_id = f"account-{old_id}"
                doc_ref = users.document(doc_id)
                batch.update(doc_ref, {"account_id": new_id})
                batch_count += 1
                
                if batch_count >= 500:
                    await batch.commit()
                    total_migrated += batch_count
                    print(f"      ✓ Migrated {total_migrated} users...")
                    batch = db.batch()
                    batch_count = 0
            
            if batch_count > 0:
                await batch.commit()
                total_migrated += batch_count
            
            print(f"   ✅ Migrated {len(users_to_migrate)} users\n")
    else:
        print(f"   ✅ All users already have prefix\n")
    
    # ========================================================================
    # 3. Migrate Facts (facts_oauth) - account_id field
    # ========================================================================
    print(f"📂 3. Migrating {facts_col}...")
    facts = db.collection(facts_col)
    
    # Find facts with account_id WITHOUT prefix
    query = facts.where(filter=firestore.FieldFilter("account_id", "!=", None))
    docs = await query.get()
    
    facts_to_migrate = []
    for doc in docs:
        account_id = doc.get("account_id")
        if account_id and not account_id.startswith("account-"):
            facts_to_migrate.append((doc.id, account_id))
    
    print(f"   Found {len(facts_to_migrate)} facts without prefix")
    
    if facts_to_migrate:
        print("\n   Sample facts:")
        for doc_id, account_id in facts_to_migrate[:3]:
            print(f"      {doc_id[:20]}...: account_id '{account_id[:20]}...' → 'account-{account_id[:20]}...'")
        
        if not dry_run:
            batch = db.batch()
            batch_count = 0
            
            for doc_id, old_id in facts_to_migrate:
                new_id = f"account-{old_id}"
                doc_ref = facts.document(doc_id)
                batch.update(doc_ref, {"account_id": new_id})
                batch_count += 1
                
                if batch_count >= 500:
                    await batch.commit()
                    total_migrated += batch_count
                    print(f"      ✓ Migrated {total_migrated} facts...")
                    batch = db.batch()
                    batch_count = 0
            
            if batch_count > 0:
                await batch.commit()
                total_migrated += batch_count
            
            print(f"   ✅ Migrated {len(facts_to_migrate)} facts\n")
    else:
        print(f"   ✅ All facts already have prefix\n")
    
    # ========================================================================
    # SUMMARY
    # ========================================================================
    print(f"{'='*70}")
    if dry_run:
        print(f"🔍 DRY RUN COMPLETE")
        print(f"{'='*70}")
        print(f"Would migrate:")
        print(f"  - {len(accounts_to_migrate)} accounts")
        print(f"  - {len(users_to_migrate)} users")
        print(f"  - {len(facts_to_migrate)} facts")
    else:
        print(f"✅ MIGRATION COMPLETE")
        print(f"{'='*70}")
        print(f"Migrated:")
        print(f"  - {len(accounts_to_migrate)} accounts")
        print(f"  - {len(users_to_migrate)} users")
        print(f"  - {len(facts_to_migrate)} facts")
        print(f"\nAll account_id fields now have 'account-' prefix!")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Add 'account-' prefix to account_id fields"
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
        asyncio.run(migrate_account_prefix(args.env, args.dry_run))
    except Exception as e:
        print(f"\n❌ Migration failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

#!/usr/bin/env python3
"""
Migration Script: Add 'state' field to legacy facts.

Session: 2026-02-16 - Deliberate Fact Management Integration
RFC: docs/10_rfcs/DELIBERATE_FACT_MANAGEMENT_RFC.md

Problem:
- Legacy facts have `is_current=True` but NO `state` field
- New code filters by `state != "superseded"`
- Legacy facts are invisible to BiographicalContext + SearchEnrichment

Solution:
- Add `state="current"` to all facts where `is_current=True` AND `state` field missing
- Keep `is_current` for backward compatibility
- Firestore auto-reindexes (no manual index rebuild needed)

Usage:
    # Dry-run (safe preview)
    python scripts/migration/add_state_field_to_legacy_facts.py --environment dev --dry-run
    
    # Real run on dev
    python scripts/migration/add_state_field_to_legacy_facts.py --environment dev
    
    # Real run on production
    python scripts/migration/add_state_field_to_legacy_facts.py --environment prod

Safety:
- Batch updates (50 docs per batch, Firestore limit 500)
- Progress logging every 50 facts
- Error handling (continue on failure)
- Dry-run mode (default)
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from google.cloud import firestore
from dotenv import load_dotenv

# Load environment
load_dotenv()


async def migrate_facts(environment: str, dry_run: bool = True):
    """
    Add 'state' field to legacy facts.
    
    Args:
        environment: 'dev' or 'prod'
        dry_run: If True, only preview changes without updating
    """
    # Initialize Firestore
    # Use GOOGLE_CLOUD_PROJECT (same as main app)
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
    
    if not project_id:
        raise ValueError(
            f"Missing GOOGLE_CLOUD_PROJECT environment variable. "
            f"Set it in .env file or export it in your shell."
        )
    
    # Collection names based on environment
    if environment == "prod":
        collection_name = "domain_facts_v2"
    else:
        collection_name = "development_domain_facts_v2"
    
    db = firestore.AsyncClient(project=project_id)
    facts_col = db.collection(collection_name)
    
    print(f"\n{'='*60}")
    print(f"🔄 Migration: Add 'state' field to legacy facts")
    print(f"{'='*60}")
    print(f"Environment: {environment}")
    print(f"Project ID: {project_id}")
    print(f"Collection: {collection_name}")
    print(f"Mode: {'DRY-RUN (preview only)' if dry_run else 'REAL RUN (will update Firestore)'}")
    print(f"{'='*60}\n")
    
    if not dry_run:
        confirm = input("⚠️  This will UPDATE Firestore documents. Continue? (yes/no): ")
        if confirm.lower() != "yes":
            print("❌ Migration cancelled by user.")
            return
    
    # STEP 1: Find legacy facts (is_current=True but no state field)
    print("📊 Step 1: Finding legacy facts...")
    print("   Query: is_current == True")
    
    # Query all facts with is_current=True
    # Note: Firestore doesn't support "field NOT EXISTS" directly,
    # so we fetch all is_current=True and filter in memory
    query = facts_col.where("is_current", "==", True)
    docs = query.stream()
    
    legacy_facts = []
    new_facts = 0
    
    async for doc in docs:
        data = doc.to_dict()
        
        # Check if 'state' field exists
        if "state" not in data:
            legacy_facts.append((doc.id, data))
        else:
            new_facts += 1
    
    total_facts = len(legacy_facts) + new_facts
    
    print(f"\n📊 Discovery Results:")
    print(f"   Total facts with is_current=True: {total_facts}")
    print(f"   ✅ New facts (already have 'state'): {new_facts}")
    print(f"   ⚠️  Legacy facts (missing 'state'): {len(legacy_facts)}")
    
    if len(legacy_facts) == 0:
        print("\n✅ No legacy facts found. Migration not needed!")
        return
    
    # STEP 2: Preview sample facts
    print(f"\n📋 Sample legacy facts (first 3):")
    for i, (doc_id, data) in enumerate(legacy_facts[:3]):
        fact_text = data.get("text", "")[:50]
        account_id = data.get("account_id", data.get("owner_id", "unknown"))[:12]
        print(f"   {i+1}. ID: {doc_id[:12]}... | account: {account_id}... | text: '{fact_text}...'")
    
    if dry_run:
        print(f"\n✅ DRY-RUN complete. Would update {len(legacy_facts)} facts.")
        print(f"   Run with '--no-dry-run' to apply changes.")
        return
    
    # STEP 3: Batch update (50 docs per batch)
    print(f"\n🔄 Step 2: Updating {len(legacy_facts)} facts...")
    
    BATCH_SIZE = 50
    updated_count = 0
    error_count = 0
    
    for i in range(0, len(legacy_facts), BATCH_SIZE):
        batch_facts = legacy_facts[i:i + BATCH_SIZE]
        batch = db.batch()
        
        for doc_id, data in batch_facts:
            doc_ref = facts_col.document(doc_id)
            
            # Update: Add state="current", keep is_current for compatibility
            batch.update(doc_ref, {
                "state": "current"
            })
        
        try:
            await batch.commit()
            updated_count += len(batch_facts)
            print(f"   ✅ Progress: {updated_count}/{len(legacy_facts)} facts updated")
        except Exception as e:
            error_count += len(batch_facts)
            print(f"   ❌ Batch failed (offset {i}): {e}")
    
    # STEP 4: Summary
    print(f"\n{'='*60}")
    print(f"✅ Migration Complete!")
    print(f"{'='*60}")
    print(f"   Updated: {updated_count} facts")
    print(f"   Errors: {error_count} facts")
    print(f"   Success rate: {(updated_count / len(legacy_facts) * 100):.1f}%")
    print(f"{'='*60}\n")
    
    if error_count > 0:
        print("⚠️  Some updates failed. Check logs above for details.")
    
    # STEP 5: Verification sample
    print("🔍 Verification: Checking sample updated facts...")
    
    sample_ids = [doc_id for doc_id, _ in legacy_facts[:3]]
    for doc_id in sample_ids:
        doc = await facts_col.document(doc_id).get()
        if doc.exists:
            data = doc.to_dict()
            has_state = "state" in data
            state_value = data.get("state", "MISSING")
            is_current = data.get("is_current", "MISSING")
            
            status = "✅" if has_state and state_value == "current" else "❌"
            print(f"   {status} {doc_id[:12]}... | state={state_value} | is_current={is_current}")
    
    print("\n✅ Migration verification complete!")


def main():
    parser = argparse.ArgumentParser(
        description="Add 'state' field to legacy facts (is_current=True but no state)"
    )
    parser.add_argument(
        "--environment",
        choices=["dev", "prod"],
        default="dev",
        help="Target environment (default: dev)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Preview changes without updating (default: True)"
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_false",
        dest="dry_run",
        help="Apply changes to Firestore (disables dry-run)"
    )
    
    args = parser.parse_args()
    
    # Run migration
    asyncio.run(migrate_facts(args.environment, args.dry_run))


if __name__ == "__main__":
    main()

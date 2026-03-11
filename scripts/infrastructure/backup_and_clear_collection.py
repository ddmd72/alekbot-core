#!/usr/bin/env python3
"""
Backup and clear specific Firestore collection

Usage:
    # Backup only (safe)
    python backup_and_clear_collection.py development_domain_facts_v2
    
    # Backup AND delete (dangerous!)
    python backup_and_clear_collection.py development_domain_facts_v2 --confirm

Author: Cline (AI)
Date: 2026-02-08
"""

import asyncio
import os
import time
import sys
from google.cloud import firestore

PROJECT_ID = os.environ["GOOGLE_CLOUD_PROJECT"]
DATABASE_ID = os.environ.get("FIRESTORE_DATABASE", "us-production")


async def backup_and_clear(source_collection: str, confirm: bool = False):
    """
    Backup collection to timestamped copy, optionally clear original
    
    Args:
        source_collection: Name of collection to backup
        confirm: If True, delete documents after backup
    """
    print("="*80)
    print(f"🔧 Firestore Collection Backup & Clear Tool")
    print(f"📍 Project: {PROJECT_ID}")
    print(f"📍 Database: {DATABASE_ID}")
    print(f"📂 Collection: {source_collection}")
    print("="*80)
    print()
    
    # Initialize Firestore client with named database
    db = firestore.AsyncClient(project=PROJECT_ID, database=DATABASE_ID)
    timestamp = int(time.time())
    backup_name = f"{source_collection}_backup_{timestamp}"
    
    # ========================================================================
    # STEP 1: BACKUP
    # ========================================================================
    print(f"📦 Step 1: Creating backup...")
    print(f"   Source: {source_collection}")
    print(f"   Backup: {backup_name}")
    print()
    
    source = db.collection(source_collection)
    backup = db.collection(backup_name)
    
    count = 0
    start_time = time.time()
    
    try:
        async for doc in source.stream():
            await backup.document(doc.id).set(doc.to_dict())
            count += 1
            if count % 100 == 0:
                elapsed = time.time() - start_time
                rate = count / elapsed if elapsed > 0 else 0
                print(f"   📝 Copied {count} documents ({rate:.1f} docs/sec)...")
    except Exception as e:
        print(f"\n❌ ERROR during backup: {e}")
        return False
    
    elapsed = time.time() - start_time
    print()
    print(f"✅ Backup complete: {count} documents in {elapsed:.1f}s")
    print()
    
    # ========================================================================
    # STEP 2: VERIFY BACKUP
    # ========================================================================
    print(f"🔍 Step 2: Verifying backup integrity...")
    
    try:
        backup_count = len([d async for d in backup.limit(count + 10).stream()])
        
        if count != backup_count:
            print(f"❌ VERIFICATION FAILED!")
            print(f"   Original: {count} docs")
            print(f"   Backup:   {backup_count} docs")
            return False
        
        print(f"✅ Verification passed: {backup_count} documents in backup")
        print()
    except Exception as e:
        print(f"❌ ERROR during verification: {e}")
        return False
    
    # ========================================================================
    # STEP 3: DELETE (OPTIONAL)
    # ========================================================================
    if not confirm:
        print("⚠️  DELETION SKIPPED (no --confirm flag)")
        print()
        print("📋 Summary:")
        print(f"   ✅ Backup created: {backup_name}")
        print(f"   ✅ Documents backed up: {count}")
        print(f"   ⏭️  Original collection: UNCHANGED")
        print()
        print("💡 To delete documents, run with --confirm flag:")
        print(f"   python {sys.argv[0]} {source_collection} --confirm")
        return True
    
    print("🗑️  Step 3: Deleting documents from original collection...")
    print(f"⚠️  WARNING: This will DELETE ALL {count} documents!")
    print()
    
    # Safety confirmation
    user_input = input(f"Type '{source_collection}' to confirm deletion: ")
    if user_input != source_collection:
        print("❌ Deletion cancelled (name mismatch)")
        return False
    
    deleted = 0
    batch_size = 400
    start_time = time.time()
    
    try:
        while True:
            docs = source.limit(batch_size).stream()
            batch = db.batch()
            batch_count = 0
            
            async for doc in docs:
                batch.delete(doc.reference)
                batch_count += 1
            
            if batch_count == 0:
                break
                
            await batch.commit()
            deleted += batch_count
            elapsed = time.time() - start_time
            rate = deleted / elapsed if elapsed > 0 else 0
            print(f"   🗑️  Deleted {deleted} documents ({rate:.1f} docs/sec)...")
        
        elapsed = time.time() - start_time
        print()
        print(f"✅ Collection cleared: {deleted} documents deleted in {elapsed:.1f}s")
        
    except Exception as e:
        print(f"\n❌ ERROR during deletion: {e}")
        print(f"   Deleted {deleted} documents before error")
        return False
    
    # ========================================================================
    # FINAL SUMMARY
    # ========================================================================
    print()
    print("="*80)
    print("🎉 OPERATION COMPLETE")
    print("="*80)
    print(f"✅ Backup: {backup_name} ({count} docs)")
    print(f"✅ Deleted: {deleted} documents from {source_collection}")
    print(f"📊 Status: Collection {source_collection} is now EMPTY")
    print()
    
    return True


def main():
    if len(sys.argv) < 2:
        print("Usage: python backup_and_clear_collection.py <collection_name> [--confirm]")
        print()
        print("Examples:")
        print("  # Backup only (safe)")
        print("  python backup_and_clear_collection.py development_domain_facts_v2")
        print()
        print("  # Backup and delete")
        print("  python backup_and_clear_collection.py development_domain_facts_v2 --confirm")
        sys.exit(1)
    
    collection = sys.argv[1]
    confirm = "--confirm" in sys.argv
    
    success = asyncio.run(backup_and_clear(collection, confirm))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

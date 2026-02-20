#!/usr/bin/env python3
"""
Analyze us-production Firestore database structure.
Lists all collections with document counts.
"""
import sys
from google.cloud import firestore

PROJECT_ID = "gen-lang-client-0554950952"
DATABASE_ID = "us-production"

def main():
    print("="*80)
    print(f"🔍 FIRESTORE DATABASE ANALYSIS")
    print(f"📦 Project: {PROJECT_ID}")
    print(f"💾 Database: {DATABASE_ID}")
    print("="*80)
    print()
    
    # Connect to Firestore
    db = firestore.Client(project=PROJECT_ID, database=DATABASE_ID)
    
    # Get all collections
    collections = list(db.collections())
    
    if not collections:
        print("❌ No collections found!")
        return
    
    print(f"📊 Found {len(collections)} collections\n")
    
    # Categorize collections
    dev_collections = []
    prod_collections = []
    other_collections = []
    
    for col in sorted(collections, key=lambda x: x.id):
        col_id = col.id
        
        # Count documents (limit to 1000 for performance)
        docs = col.limit(1).stream()
        has_docs = len(list(docs)) > 0
        
        # Try to get actual count (expensive, so we skip it)
        count_str = "✅ Has data" if has_docs else "❌ Empty"
        
        if col_id.startswith('development_'):
            dev_collections.append((col_id, count_str))
        elif col_id.startswith('test_'):
            other_collections.append((col_id, count_str))
        else:
            prod_collections.append((col_id, count_str))
    
    # Print results
    if dev_collections:
        print("🔧 DEVELOPMENT COLLECTIONS (development_*):")
        print("-" * 80)
        for col_id, status in dev_collections:
            print(f"  {status:15} {col_id}")
        print()
    
    if prod_collections:
        print("🏭 PRODUCTION COLLECTIONS (no prefix):")
        print("-" * 80)
        for col_id, status in prod_collections:
            print(f"  {status:15} {col_id}")
        print()
    
    if other_collections:
        print("🧪 TEST/OTHER COLLECTIONS:")
        print("-" * 80)
        for col_id, status in other_collections:
            print(f"  {status:15} {col_id}")
        print()
    
    # Generate migration mapping
    print("="*80)
    print("📋 MIGRATION MAPPING (DEV → PROD)")
    print("="*80)
    print()
    
    mappings = []
    for col_id, _ in dev_collections:
        if col_id.startswith('development_'):
            prod_name = col_id.replace('development_', '', 1)
            mappings.append((col_id, prod_name))
    
    if mappings:
        for source, target in mappings:
            print(f"  {source:50} → {target}")
    else:
        print("  ❌ No development collections found to migrate")
    
    print()
    print("="*80)
    print(f"✅ Analysis complete! Total collections: {len(collections)}")
    print("="*80)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ Error: {e}", file=sys.stderr)
        sys.exit(1)

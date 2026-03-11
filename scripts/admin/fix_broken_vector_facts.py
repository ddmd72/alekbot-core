"""
Fix Broken Vector Facts - Session 2026-02-09

Problem: FactWriteService saved vectors as map {0: val, 1: val, ...} instead of Vector array.
Root Cause: Missing Vector() wrapper in FirestoreFactRepository.add_fact()
Impact: 3 facts in PRODUCTION with broken vector fields (cannot be used in vector search)

This script:
1. Loads 3 specific facts by ID
2. Converts map structure → list[float]
3. Wraps in Vector() for proper Firestore serialization
4. Updates documents atomically

Safety:
- Reads original data first (for rollback if needed)
- Validates structure before update
- Logs all changes
- Production-safe (only updates specified IDs)

Usage:
    # Dry-run (shows what would be changed, no writes)
    python scripts/admin/fix_broken_vector_facts.py --dry-run

    # Live migration (updates Firestore)
    python scripts/admin/fix_broken_vector_facts.py --live

Environment: us-production
Collection: development_domain_facts_v2
"""

import asyncio
import argparse
from google.cloud import firestore
from google.cloud.firestore_v1.vector import Vector
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from src.utils.logger import logger


# 3 broken fact IDs from user
BROKEN_FACT_IDS = [
    "7482d369-bd5c-4a9e-8159-8537718dea2a",
    "05e68bc8-8352-4e1d-9b93-15fc88cd5ed9",
    "5e1a615d-6b7a-42e1-873f-e1014ca353f8"
]

COLLECTION_NAME = "development_domain_facts_v2"


def map_to_list(vector_map: dict) -> list:
    """
    Convert Firestore map {0: val, 1: val, ...} → list[float].
    
    Args:
        vector_map: Dict with string keys "0", "1", "2", ...
        
    Returns:
        List of floats in correct order
    """
    if not isinstance(vector_map, dict):
        raise ValueError(f"Expected dict, got {type(vector_map)}")
    
    # Sort by numeric key
    sorted_keys = sorted(vector_map.keys(), key=lambda k: int(k))
    vector_list = [float(vector_map[k]) for k in sorted_keys]
    
    return vector_list


async def fix_broken_vectors(dry_run: bool = True):
    """
    Fix 3 facts with broken vector fields.
    
    Args:
        dry_run: If True, only print changes without updating Firestore
    """
    logger.info(f"🔧 [FixVectors] Starting migration (dry_run={dry_run})")
    logger.info(f"📊 [FixVectors] Target: {len(BROKEN_FACT_IDS)} facts in {COLLECTION_NAME}")
    
    # Initialize Firestore client
    db = firestore.AsyncClient(database="us-production")
    facts_col = db.collection(COLLECTION_NAME)
    
    fixed_count = 0
    skipped_count = 0
    
    for fact_id in BROKEN_FACT_IDS:
        logger.info(f"\n{'='*80}")
        logger.info(f"🔍 [FixVectors] Processing fact: {fact_id}")
        
        try:
            # Load document
            doc = await facts_col.document(fact_id).get()
            
            if not doc.exists:
                logger.error(f"❌ [FixVectors] Document not found: {fact_id}")
                skipped_count += 1
                continue
            
            data = doc.to_dict()
            needs_fix = False
            fixed_fields = []
            
            # Check vector field
            if 'vector' in data:
                if isinstance(data['vector'], dict):
                    logger.warning(f"⚠️  [FixVectors] vector is map (broken): {len(data['vector'])} dimensions")
                    vector_list = map_to_list(data['vector'])
                    logger.info(f"   ✓ Converted to list: {len(vector_list)} floats")
                    
                    if dry_run:
                        logger.info(f"   [DRY-RUN] Would wrap in Vector() and save")
                    else:
                        data['vector'] = Vector(vector_list)
                    
                    needs_fix = True
                    fixed_fields.append('vector')
                elif isinstance(data['vector'], Vector):
                    logger.info(f"✅ [FixVectors] vector already correct (Vector type)")
                elif isinstance(data['vector'], list):
                    logger.info(f"⚠️  [FixVectors] vector is list (needs Vector wrapper)")
                    if not dry_run:
                        data['vector'] = Vector(data['vector'])
                    needs_fix = True
                    fixed_fields.append('vector (wrap)')
                else:
                    logger.warning(f"❓ [FixVectors] vector unknown type: {type(data['vector'])}")
            
            # Check tags_vector field
            if 'tags_vector' in data:
                if isinstance(data['tags_vector'], dict):
                    logger.warning(f"⚠️  [FixVectors] tags_vector is map (broken): {len(data['tags_vector'])} dimensions")
                    tags_list = map_to_list(data['tags_vector'])
                    logger.info(f"   ✓ Converted to list: {len(tags_list)} floats")
                    
                    if dry_run:
                        logger.info(f"   [DRY-RUN] Would wrap in Vector() and save")
                    else:
                        data['tags_vector'] = Vector(tags_list)
                    
                    needs_fix = True
                    fixed_fields.append('tags_vector')
                elif isinstance(data['tags_vector'], Vector):
                    logger.info(f"✅ [FixVectors] tags_vector already correct (Vector type)")
                elif isinstance(data['tags_vector'], list):
                    logger.info(f"⚠️  [FixVectors] tags_vector is list (needs Vector wrapper)")
                    if not dry_run:
                        data['tags_vector'] = Vector(data['tags_vector'])
                    needs_fix = True
                    fixed_fields.append('tags_vector (wrap)')
            
            # Check metadata_vector field
            if 'metadata_vector' in data:
                if isinstance(data['metadata_vector'], dict):
                    logger.warning(f"⚠️  [FixVectors] metadata_vector is map (broken): {len(data['metadata_vector'])} dimensions")
                    meta_list = map_to_list(data['metadata_vector'])
                    logger.info(f"   ✓ Converted to list: {len(meta_list)} floats")
                    
                    if dry_run:
                        logger.info(f"   [DRY-RUN] Would wrap in Vector() and save")
                    else:
                        data['metadata_vector'] = Vector(meta_list)
                    
                    needs_fix = True
                    fixed_fields.append('metadata_vector')
                elif isinstance(data['metadata_vector'], Vector):
                    logger.info(f"✅ [FixVectors] metadata_vector already correct (Vector type)")
                elif isinstance(data['metadata_vector'], list):
                    logger.info(f"⚠️  [FixVectors] metadata_vector is list (needs Vector wrapper)")
                    if not dry_run:
                        data['metadata_vector'] = Vector(data['metadata_vector'])
                    needs_fix = True
                    fixed_fields.append('metadata_vector (wrap)')
            
            # Update document if needed
            if needs_fix:
                logger.info(f"📝 [FixVectors] Fields to fix: {', '.join(fixed_fields)}")
                
                if dry_run:
                    logger.info(f"   [DRY-RUN] Would update document {fact_id[:8]}...")
                else:
                    await doc.reference.set(data)
                    logger.info(f"✅ [FixVectors] Updated document {fact_id[:8]}...")
                
                fixed_count += 1
            else:
                logger.info(f"⏭️  [FixVectors] No fix needed for {fact_id[:8]}")
                skipped_count += 1
        
        except Exception as e:
            logger.error(f"❌ [FixVectors] Error processing {fact_id}: {e}", exc_info=True)
            skipped_count += 1
    
    logger.info(f"\n{'='*80}")
    logger.info(f"✅ [FixVectors] Migration complete!")
    logger.info(f"   Fixed: {fixed_count} facts")
    logger.info(f"   Skipped: {skipped_count} facts")
    
    if dry_run:
        logger.info(f"\n⚠️  DRY-RUN MODE: No changes were written to Firestore")
        logger.info(f"   Run with --live to apply changes")


def main():
    parser = argparse.ArgumentParser(
        description="Fix broken vector facts in Firestore (map → Vector array)"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Apply changes to Firestore (default: dry-run)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Show changes without applying (default)"
    )
    
    args = parser.parse_args()
    
    # If --live specified, disable dry-run
    dry_run = not args.live
    
    if not dry_run:
        print("\n" + "="*80)
        print("⚠️  WARNING: PRODUCTION DATABASE MIGRATION")
        print("="*80)
        print(f"Database: us-production")
        print(f"Collection: {COLLECTION_NAME}")
        print(f"Documents to update: {len(BROKEN_FACT_IDS)}")
        print("\nThis will UPDATE vector fields in PRODUCTION!")
        print("="*80)
        
        confirm = input("\nType 'YES' to proceed: ")
        if confirm != "YES":
            print("❌ Migration cancelled")
            return
        
        print("\n🚀 Starting live migration...\n")
    
    asyncio.run(fix_broken_vectors(dry_run=dry_run))


if __name__ == "__main__":
    main()

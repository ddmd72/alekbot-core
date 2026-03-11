#!/usr/bin/env python3
"""
Migration Script: Add 'context_priority_rank' to development_domain_facts_v2.

Problem:
- get_biographical_context fetched ALL current facts (O(N) scan)
- New get_active_facts_ordered uses ORDER BY context_priority_rank for bounded queries
- Existing facts missing this field → Firestore falls back to full scan

Solution:
- Add context_priority_rank: int derived from context_priority string
- critical→1, high→2, medium→3, low→4, archival→5 (default: 3)

Scope: development_domain_facts_v2 only (prod will be rebuilt from dev).

Usage:
    # Dry-run (preview)
    python scripts/migration/add_priority_rank_to_facts.py --dry-run

    # Real run
    python scripts/migration/add_priority_rank_to_facts.py
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from google.cloud import firestore
from dotenv import load_dotenv

load_dotenv()

PRIORITY_RANK = {
    "critical": 1,
    "high":     2,
    "medium":   3,
    "low":      4,
    "archival": 5,
}
COLLECTION = "development_domain_facts_v2"
BATCH_SIZE = 50


async def migrate(dry_run: bool = True):
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        raise ValueError("Missing GOOGLE_CLOUD_PROJECT in .env")

    database = os.getenv("FIRESTORE_DATABASE", "us-production")

    db = firestore.AsyncClient(project=project_id, database=database)
    col = db.collection(COLLECTION)

    print(f"\n{'='*60}")
    print(f"🔄 Migration: Add context_priority_rank")
    print(f"{'='*60}")
    print(f"Project:    {project_id}")
    print(f"Database:   {database}")
    print(f"Collection: {COLLECTION}")
    print(f"Mode:       {'DRY-RUN' if dry_run else 'REAL RUN'}")
    print(f"{'='*60}\n")

    if not dry_run:
        confirm = input("⚠️  This will UPDATE Firestore documents. Continue? (yes/no): ")
        if confirm.lower() != "yes":
            print("❌ Cancelled.")
            return

    # Fetch all docs that already have state (i.e. are not purely legacy)
    print("📊 Scanning collection...")
    docs_stream = col.stream()

    needs_update = []
    already_done = 0

    async for doc in docs_stream:
        data = doc.to_dict()
        if "context_priority_rank" in data:
            already_done += 1
        else:
            priority_str = data.get("context_priority") or "medium"
            rank = PRIORITY_RANK.get(priority_str, 3)
            needs_update.append((doc.id, rank, priority_str))

    print(f"\n📊 Scan results:")
    print(f"   Already have rank: {already_done}")
    print(f"   Need update:       {len(needs_update)}")

    if not needs_update:
        print("\n✅ Nothing to migrate.")
        return

    print(f"\n📋 Sample (first 5):")
    for doc_id, rank, priority in needs_update[:5]:
        print(f"   {doc_id[:16]}...  {priority} → rank={rank}")

    if dry_run:
        print(f"\n✅ DRY-RUN done. Would update {len(needs_update)} docs.")
        print("   Run without --dry-run to apply.")
        return

    # Batch update
    print(f"\n🔄 Updating {len(needs_update)} docs in batches of {BATCH_SIZE}...")
    updated = 0
    errors = 0

    for i in range(0, len(needs_update), BATCH_SIZE):
        batch_slice = needs_update[i:i + BATCH_SIZE]
        batch = db.batch()
        for doc_id, rank, _ in batch_slice:
            doc_ref = col.document(doc_id)
            batch.update(doc_ref, {"context_priority_rank": rank})
        try:
            await batch.commit()
            updated += len(batch_slice)
            print(f"   ✅ {updated}/{len(needs_update)} updated")
        except Exception as e:
            errors += len(batch_slice)
            print(f"   ❌ Batch error: {e}")

    print(f"\n{'='*60}")
    print(f"✅ Done: {updated} updated, {errors} errors")
    print(f"{'='*60}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Add context_priority_rank to facts")
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="Preview changes without writing (default: False)")
    args = parser.parse_args()
    asyncio.run(migrate(dry_run=args.dry_run))

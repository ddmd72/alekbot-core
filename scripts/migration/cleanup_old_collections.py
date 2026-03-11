#!/usr/bin/env python3
"""
Firestore Cleanup Script - Phase 5-1
=====================================
Deletes old collections before migrating to dual-collection architecture.

Usage:
    python cleanup_old_collections.py --env dev
    python cleanup_old_collections.py --env dev --dry-run

Collections to be deleted:
    {env}_prompt_tokens_v3  (replaced by system_tokens + user_tokens)
"""

import argparse
import sys
from google.cloud import firestore


def delete_collection(db: firestore.Client, collection_name: str, batch_size: int = 500, dry_run: bool = False) -> int:
    """
    Delete all documents in a Firestore collection.

    Args:
        db: Firestore client
        collection_name: Name of collection to delete
        batch_size: Number of documents to delete per batch
        dry_run: If True, only show what would be deleted

    Returns:
        Number of documents deleted
    """
    coll_ref = db.collection(collection_name)
    deleted = 0

    while True:
        # Get batch of documents
        docs = coll_ref.limit(batch_size).stream()
        doc_count = 0

        batch = db.batch()
        for doc in docs:
            if dry_run:
                print(f"  [DRY RUN] Would delete: {doc.id}")
            else:
                batch.delete(doc.reference)
            doc_count += 1
            deleted += 1

        if doc_count == 0:
            break

        if not dry_run:
            batch.commit()
            print(f"  Deleted {doc_count} documents...")

    return deleted


def main():
    parser = argparse.ArgumentParser(
        description="Cleanup old Firestore collections (Phase 5-1)"
    )
    parser.add_argument(
        "--env",
        required=True,
        choices=["dev", "staging", "prod"],
        help="Environment (dev/staging/prod)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry run mode - show what would be deleted without actually deleting",
    )
    parser.add_argument(
        "--project-id",
        help="GCP project ID (optional, uses default credentials if not provided)",
    )

    args = parser.parse_args()

    # Initialize Firestore client
    if args.dry_run:
        print("=" * 70)
        print("DRY RUN MODE - No data will be deleted")
        print("=" * 70)
        # Still need client for listing docs
        if args.project_id:
            db = firestore.Client(project=args.project_id)
        else:
            db = firestore.Client()
    else:
        if args.project_id:
            db = firestore.Client(project=args.project_id)
        else:
            db = firestore.Client()
        print("=" * 70)
        print(f"⚠️  CLEANUP MODE - Environment: {args.env}")
        print("=" * 70)

    # Collections to delete (Day 1.4 - Token split only)
    collections_to_delete = [
        f"{args.env}_prompt_tokens_v3",  # Old single token collection
    ]

    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}Collections to delete:")
    for coll in collections_to_delete:
        print(f"  - {coll}")

    if not args.dry_run:
        confirm = input("\n⚠️  Are you sure you want to delete these collections? (yes/no): ")
        if confirm.lower() != "yes":
            print("Cleanup cancelled.")
            sys.exit(0)

    # Run cleanup
    try:
        total_deleted = 0
        for collection_name in collections_to_delete:
            print(f"\n{'[DRY RUN] ' if args.dry_run else ''}Processing {collection_name}...")

            # Check if collection exists
            docs = db.collection(collection_name).limit(1).stream()
            has_docs = False
            for _ in docs:
                has_docs = True
                break

            if not has_docs:
                print(f"  ⚠️  Collection is empty or doesn't exist, skipping...")
                continue

            deleted = delete_collection(db, collection_name, dry_run=args.dry_run)
            total_deleted += deleted

            if not args.dry_run:
                print(f"  ✓ Deleted {deleted} documents from {collection_name}")

        print("\n" + "=" * 70)
        print("CLEANUP SUMMARY")
        print("=" * 70)
        print(f"  Environment: {args.env}")
        print(f"  Total documents deleted: {total_deleted}")

        if args.dry_run:
            print("\n⚠️  This was a DRY RUN - no data was deleted from Firestore")
        else:
            print("\n✅ Cleanup completed successfully!")
            print("\n⚠️  Remember to run the migration script next:")
            print(f"     python migrate_tokens_split.py --env {args.env}")

    except Exception as e:
        print(f"\n❌ Cleanup failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

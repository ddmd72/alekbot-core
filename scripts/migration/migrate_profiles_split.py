#!/usr/bin/env python3
"""
Profile Split Migration Script - Phase 5-1, Day 2.4
====================================================
    Migrates agent profiles from agent_profiles.yaml to dual-collection architecture:
- SYSTEM/AGENT profiles -> {env}_agent_profiles
- USER/ACCOUNT overrides -> {env}_user_token_overrides

Usage:
    python migrate_profiles_split.py --env dev
    python migrate_profiles_split.py --env dev --dry-run

Split Logic:
    Profiles are routed based on owner_type:
    - SYSTEM/AGENT: agent_profiles collection (admin-controlled)
    - USER/ACCOUNT: user_token_overrides collection (user-modifiable)
"""

import argparse
import sys
from pathlib import Path
from typing import Any, Dict

import yaml
from google.cloud import firestore


def load_yaml(file_path: Path) -> Any:
    """Load YAML file."""
    with open(file_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def migrate_profiles_split(
    db: firestore.Client,
    env: str,
    profiles_path: Path,
    dry_run: bool = False
) -> Dict[str, int]:
    """
    Migrate agent profiles to dual-collection architecture.

    Args:
        db: Firestore client
        env: Environment name (dev/staging/prod)
        profiles_path: Path to agent_profiles.yaml
        dry_run: If True, show what would be migrated without writing

    Returns:
        Dict with counts: {"profiles": N, "overrides": N, "total": N}
    """
    profiles = load_yaml(profiles_path)

    # Collection names (Phase 5-1)
    profiles_collection = f"{env}_prompt_agent_profiles"
    overrides_collection = f"{env}_prompt_user_token_overrides"

    counts = {"profiles": 0, "overrides": 0, "total": 0, "errors": 0}

    for profile in profiles:
        profile_id = profile["id"]
        owner_type = profile["owner_type"]
        owner_value = profile["owner_value"]
        agent_type = profile.get("agent_type")  # Optional: only for SYSTEM/AGENT profiles
        blueprint_id = profile["blueprint_id"]
        slots = profile.get("slots", [])
        metadata = profile.get("metadata", {})

        # Determine target collection based on owner_type
        if owner_type in ["SYSTEM", "AGENT"]:
            collection_name = profiles_collection
            id_field = "profile_id"
            counts["profiles"] += 1
        elif owner_type in ["USER", "ACCOUNT"]:
            collection_name = overrides_collection
            id_field = "override_id"
            counts["overrides"] += 1
        else:
            print(f"  ⚠️  Unknown owner_type: {owner_type} for profile {profile_id}")
            counts["errors"] += 1
            continue

        # Prepare document data
        doc_data = {
            id_field: profile_id,
            "blueprint_id": blueprint_id,
            "owner_type": owner_type,
            "owner_value": owner_value,
            "slots": slots,
            "metadata": metadata,
        }

        # Add agent_type if present (typically for SYSTEM/AGENT profiles)
        if agent_type:
            doc_data["agent_type"] = agent_type

        if dry_run:
            print(f"  [DRY RUN] Would create {owner_type} in {collection_name}: {profile_id}")
        else:
            db.collection(collection_name).document(profile_id).set(doc_data)
            print(f"  ✓ Created {owner_type} in {collection_name}: {profile_id}")

        counts["total"] += 1

    return counts


def main():
    parser = argparse.ArgumentParser(
        description="Migrate agent profiles to dual-collection architecture (Phase 5-1)"
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
        help="Dry run mode - show what would be migrated without writing",
    )
    parser.add_argument(
        "--project-id",
        help="GCP project ID (optional, uses default credentials if not provided)",
    )

    args = parser.parse_args()

    # Setup paths
    script_dir = Path(__file__).parent
    profiles_path = script_dir / "agent_profiles.yaml"

    # Validate files exist
    if not profiles_path.exists():
        print(f"❌ Error: Missing required file: {profiles_path}")
        sys.exit(1)

    # Initialize Firestore client
    if args.dry_run:
        print("=" * 70)
        print("DRY RUN MODE - No data will be written")
        print("=" * 70)
        db = None
    else:
        if args.project_id:
            db = firestore.Client(project=args.project_id)
        else:
            db = firestore.Client()
        print("=" * 70)
        print(f"MIGRATING TO FIRESTORE - Environment: {args.env}")
        print("=" * 70)

    # Run migration
    try:
        counts = migrate_profiles_split(
            db=db,
            env=args.env,
            profiles_path=profiles_path,
            dry_run=args.dry_run
        )

        print("\n" + "=" * 70)
        print("MIGRATION SUMMARY")
        print("=" * 70)
        print(f"  Environment: {args.env}")
        print(f"  Agent profiles (SYSTEM/AGENT): {counts['profiles']}")
        print(f"  User overrides (USER/ACCOUNT): {counts['overrides']}")
        print(f"  Total profiles migrated: {counts['total']}")
        if counts["errors"] > 0:
            print(f"  ⚠️  Errors encountered: {counts['errors']}")

        if args.dry_run:
            print("\n⚠️  This was a DRY RUN - no data was written to Firestore")
        else:
            print("\n✅ Profile split migration completed successfully!")
            print("\nNext steps:")
            print("  1. Verify data in Firestore console")
            print("  2. Continue with Day 3.x (SlotExclusion enhancement)")

    except Exception as e:
        print(f"\n❌ Migration failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Token Split Migration Script - Phase 5-1, Day 1.5
==================================================
Migrates tokens from tokens_library.yaml to dual-collection architecture:
- SYSTEM tokens (20) -> {env}_prompt_system_tokens
- USER tokens (19) -> {env}_prompt_user_tokens

Usage:
    python migrate_tokens_split.py --env dev
    python migrate_tokens_split.py --env dev --dry-run

Split Logic:
    Tokens are classified based on override_by metadata:
    - SYSTEM: override_by does NOT contain USER or ACCOUNT
    - USER: override_by contains USER or ACCOUNT
"""

import argparse
import sys
from pathlib import Path
from typing import Any, List, Dict

import yaml
from google.cloud import firestore


def load_yaml(file_path: Path) -> Any:
    """Load YAML file."""
    with open(file_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def classify_token(token_data: Dict[str, Any]) -> str:
    """
    Classify token as SYSTEM or USER based on override_by metadata.

    Args:
        token_data: Token dictionary with metadata field

    Returns:
        "system" or "user"
    """
    override_by = token_data.get("metadata", {}).get("override_by", [])

    # SYSTEM: no USER or ACCOUNT in override_by
    # USER: has USER or ACCOUNT in override_by
    if "USER" in override_by or "ACCOUNT" in override_by:
        return "user"
    else:
        return "system"


def migrate_tokens_split(
    db: firestore.Client,
    env: str,
    tokens_library_path: Path,
    system_tokens_path: Path,
    user_tokens_path: Path,
    dry_run: bool = False
) -> Dict[str, int]:
    """
    Migrate tokens to dual-collection architecture.

    Args:
        db: Firestore client
        env: Environment name (dev/staging/prod)
        tokens_library_path: Path to tokens_library.yaml (full token data)
        system_tokens_path: Path to system_tokens.yaml (token ID list)
        user_tokens_path: Path to user_tokens.yaml (token ID list)
        dry_run: If True, show what would be migrated without writing

    Returns:
        Dict with counts: {"system": N, "user": N, "total": N}
    """
    # Load token library (full data)
    tokens_library = load_yaml(tokens_library_path)
    token_map = {token["id"]: token for token in tokens_library}

    # Load split lists (token IDs only)
    system_tokens_list = load_yaml(system_tokens_path)["tokens"]
    user_tokens_list = load_yaml(user_tokens_path)["tokens"]

    # Collection names
    system_collection = f"{env}_prompt_system_tokens"
    user_collection = f"{env}_prompt_user_tokens"

    counts = {"system": 0, "user": 0, "total": 0, "errors": 0}

    # Migrate SYSTEM tokens
    print(f"\n{'[DRY RUN] ' if dry_run else ''}Migrating SYSTEM tokens to {system_collection}...")
    for token_id in system_tokens_list:
        if token_id not in token_map:
            print(f"  ⚠️  Token not found in library: {token_id}")
            counts["errors"] += 1
            continue

        token_data = token_map[token_id]

        # Validate classification
        classification = classify_token(token_data)
        if classification != "system":
            print(f"  ⚠️  Token {token_id} should be USER but listed in SYSTEM list!")
            counts["errors"] += 1
            continue

        # Ensure metadata has validation field
        metadata = token_data.get("metadata", {})
        if "validation" not in metadata:
            metadata["validation"] = {
                "risk_level": "SAFE",
                "risk_score": 0.0,
                "patterns_detected": [],
                "action_taken": "passed",
                "adapter": "noop",
                "context": "token_creation",
                "zone": "trusted"
            }

        doc_data = {
            "token_id": token_id,
            "category": token_data["category"],
            "class": token_data.get("class"),
            "content": token_data["content"],
            "metadata": metadata,
            "created_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }

        if dry_run:
            print(f"  [DRY RUN] Would create: {token_id}")
        else:
            db.collection(system_collection).document(token_id).set(doc_data)
            print(f"  ✓ Created: {token_id}")

        counts["system"] += 1
        counts["total"] += 1

    # Migrate USER tokens
    print(f"\n{'[DRY RUN] ' if dry_run else ''}Migrating USER tokens to {user_collection}...")
    for token_id in user_tokens_list:
        if token_id not in token_map:
            print(f"  ⚠️  Token not found in library: {token_id}")
            counts["errors"] += 1
            continue

        token_data = token_map[token_id]

        # Validate classification
        classification = classify_token(token_data)
        if classification != "user":
            print(f"  ⚠️  Token {token_id} should be SYSTEM but listed in USER list!")
            counts["errors"] += 1
            continue

        # Ensure metadata has validation field
        metadata = token_data.get("metadata", {})
        if "validation" not in metadata:
            metadata["validation"] = {
                "risk_level": "SAFE",
                "risk_score": 0.0,
                "patterns_detected": [],
                "action_taken": "passed",
                "adapter": "noop",
                "context": "token_creation",
                "zone": "trusted"
            }

        doc_data = {
            "token_id": token_id,
            "category": token_data["category"],
            "class": token_data.get("class"),
            "content": token_data["content"],
            "metadata": metadata,
            "created_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }

        if dry_run:
            print(f"  [DRY RUN] Would create: {token_id}")
        else:
            db.collection(user_collection).document(token_id).set(doc_data)
            print(f"  ✓ Created: {token_id}")

        counts["user"] += 1
        counts["total"] += 1

    return counts


def main():
    parser = argparse.ArgumentParser(
        description="Migrate tokens to dual-collection architecture (Phase 5-1)"
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
    tokens_library_path = script_dir / "tokens_library.yaml"
    system_tokens_path = script_dir / "system_tokens.yaml"
    user_tokens_path = script_dir / "user_tokens.yaml"

    # Validate files exist
    missing_files = []
    for path in [tokens_library_path, system_tokens_path, user_tokens_path]:
        if not path.exists():
            missing_files.append(str(path))

    if missing_files:
        print(f"❌ Error: Missing required files:")
        for f in missing_files:
            print(f"  - {f}")
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
        counts = migrate_tokens_split(
            db=db,
            env=args.env,
            tokens_library_path=tokens_library_path,
            system_tokens_path=system_tokens_path,
            user_tokens_path=user_tokens_path,
            dry_run=args.dry_run
        )

        print("\n" + "=" * 70)
        print("MIGRATION SUMMARY")
        print("=" * 70)
        print(f"  Environment: {args.env}")
        print(f"  SYSTEM tokens migrated: {counts['system']}")
        print(f"  USER tokens migrated: {counts['user']}")
        print(f"  Total tokens migrated: {counts['total']}")
        if counts["errors"] > 0:
            print(f"  ⚠️  Errors encountered: {counts['errors']}")

        if args.dry_run:
            print("\n⚠️  This was a DRY RUN - no data was written to Firestore")
        else:
            print("\n✅ Token split migration completed successfully!")
            print("\nNext steps:")
            print("  1. Verify data in Firestore console")
            print("  2. Continue with Day 2.x (Profile split)")

    except Exception as e:
        print(f"\n❌ Migration failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

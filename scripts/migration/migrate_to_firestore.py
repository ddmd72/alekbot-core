#!/usr/bin/env python3
"""
Firestore Migration Script - Prompt Design System v3
=====================================================
Loads tokens, blueprints, and agent profiles into Firestore.

Usage:
    python migrate_to_firestore.py --env dev
    python migrate_to_firestore.py --env prod --dry-run

Collections created:
    {env}_prompt_tokens
    {env}_prompt_blueprints
    {env}_agent_profiles
"""

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml
from google.cloud import firestore


def load_yaml(file_path: Path) -> Any:
    """Load YAML file."""
    with open(file_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_groovy(file_path: Path) -> str:
    """Load Groovy template file."""
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read()


def migrate_tokens(db: firestore.Client, env: str, tokens_path: Path, dry_run: bool = False) -> int:
    """
    Migrate tokens from tokens_library.yaml to Firestore.

    Collection: {env}_prompt_tokens
    Document structure:
        - id: token_id (e.g., HUMOR_PRESET_RANEVSKAYA)
        - category: str (e.g., "humor_engine")
        - content: str (Groovy DSL content)
        - metadata: dict
    """
    collection_name = f"{env}_prompt_tokens"
    tokens = load_yaml(tokens_path)

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Migrating {len(tokens)} tokens to {collection_name}...")

    count = 0
    for token in tokens:
        token_id = token["id"]
        doc_data = {
            "token_id": token_id,  # Repository expects "token_id" not "id"
            "category": token["category"],
            "content": token["content"],
            "metadata": token.get("metadata", {}),
        }

        if dry_run:
            print(f"  [DRY RUN] Would create: {token_id}")
        else:
            db.collection(collection_name).document(token_id).set(doc_data)
            print(f"  ✓ Created: {token_id}")

        count += 1

    return count


def migrate_blueprint(db: firestore.Client, env: str, blueprint_path: Path, profiles_path: Path, dry_run: bool = False) -> int:
    """
    Migrate universal blueprint to Firestore.

    Collection: {env}_prompt_blueprints
    Document structure:
        - id: blueprint_id (e.g., universal_agent_v1)
        - template: str (Groovy DSL with {{CLASS}} placeholders)
        - classes: dict (BlueprintClass definitions)
        - metadata: dict
    """
    collection_name = f"{env}_prompt_blueprints"
    template_content = load_groovy(blueprint_path)

    # Load profiles to get default tokens from smart agent (most complete profile)
    profiles = load_yaml(profiles_path)
    smart_profile = next((p for p in profiles if p["owner_value"] == "smart"), None)
    default_tokens = {}
    if smart_profile:
        for slot in smart_profile.get("slots", []):
            if slot.get("type") == "token":
                default_tokens[slot.get("value")] = slot.get("value")

    # Extract class definitions from template
    # Classes are all {{TOKEN_NAME}} placeholders
    import re
    slot_pattern = r'\{\{([A-Z_]+)\}\}'
    slots_found = set(re.findall(slot_pattern, template_content))

    # Define class schemas in FirestoreBlueprintRepository format
    # Expected format: {allowed_token_categories, overridable_by, default_token}
    class_schemas = {}
    for slot_name in slots_found:
        # Map slot names to categories, overrides, and defaults
        # Get default token from smart profile (fallback to placeholder)
        default_token = default_tokens.get(slot_name, f"DEFAULT_{slot_name}")

        # Determine category and overridable_by based on slot name pattern
        if "HUMOR" in slot_name:
            category = ["humor_engine"]
            overridable_by = ["USER", "ACCOUNT", "AGENT", "SYSTEM"]
        elif "ARCHETYPE" in slot_name:
            category = ["archetype"]
            overridable_by = ["USER", "ACCOUNT", "AGENT", "SYSTEM"]
        elif "VOICE" in slot_name:
            category = ["voice"]
            overridable_by = ["USER", "ACCOUNT", "AGENT", "SYSTEM"]
        elif "VIBE" in slot_name:
            category = ["vibe"]
            overridable_by = ["USER", "ACCOUNT", "AGENT", "SYSTEM"]
        elif "RESPONSE" in slot_name:
            category = ["response_style"]
            overridable_by = ["USER", "ACCOUNT", "AGENT", "SYSTEM"]
        elif "COGNITIVE_PROCESS" in slot_name:
            category = ["cognitive_process"]
            overridable_by = ["AGENT", "SYSTEM"]
        elif "OUTPUT_FORMAT" in slot_name:
            category = ["output_format"]
            overridable_by = ["AGENT", "ACCOUNT", "SYSTEM"]
        elif "PROTOCOL" in slot_name:
            category = ["protocol"]
            overridable_by = ["SYSTEM", "AGENT"]
        elif "DIRECTIVE" in slot_name:
            category = ["final_directive"]
            overridable_by = ["SYSTEM"]
        elif "POLICY" in slot_name:
            category = ["policy"]
            overridable_by = ["SYSTEM"]
        elif "MOTTO" in slot_name:
            category = ["motto"]
            overridable_by = ["USER", "ACCOUNT", "SYSTEM"]
        elif "BEHAVIOR_GUIDE" in slot_name:
            category = ["behavior_guide"]
            overridable_by = ["SYSTEM", "AGENT"]
        elif "FEW_SHOT_EXAMPLES" in slot_name:
            category = ["few_shot_examples"]
            overridable_by = ["SYSTEM"]
        else:
            # Generic fallback
            category = ["unknown"]
            overridable_by = ["SYSTEM"]

        # Create slot schema in expected format
        class_schemas[slot_name] = {
            "allowed_token_categories": category,
            "overridable_by": overridable_by,
            "default_token": default_token
        }

    blueprint_id = "universal_agent_v1"
    doc_data = {
        "blueprint_id": blueprint_id,  # Repository expects "blueprint_id"
        "template": template_content,
        "classes": class_schemas,
        "metadata": {
            "created_at": "2026-02-02",
            "status": "CANONICAL",
            "description": "Universal blueprint for all agents in Prompt Design System v3",
            "version": "3.0",
        }
    }

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Migrating blueprint to {collection_name}...")
    print(f"  Blueprint ID: {blueprint_id}")
    print(f"  Classes found: {len(class_schemas)}")

    if dry_run:
        print(f"  [DRY RUN] Would create: {blueprint_id}")
    else:
        db.collection(collection_name).document(blueprint_id).set(doc_data)
        print(f"  ✓ Created: {blueprint_id}")

    return 1


def migrate_agent_profiles(db: firestore.Client, env: str, profiles_path: Path, dry_run: bool = False) -> int:
    """
    Migrate agent profiles to Firestore.

    Collection: {env}_agent_profiles
    Document structure:
        - id: profile_id (e.g., smart_agent_system_default)
        - owner_type: str (SYSTEM / AGENT / ACCOUNT / USER)
        - owner_value: str (agent type or user/account ID)
        - blueprint_id: str (reference to blueprint)
        - slots: list[ProfileSlot]
        - metadata: dict
    """
    collection_name = f"{env}_agent_profiles"
    profiles = load_yaml(profiles_path)

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Migrating {len(profiles)} agent profiles to {collection_name}...")

    count = 0
    for profile in profiles:
        original_id = profile["id"]
        owner_type = profile["owner_type"]
        owner_value = profile["owner_value"]
        blueprint_id = profile["blueprint_id"]

        # Generate composite profile_id as expected by repository
        # Format: {blueprint_id}_{owner_type}_{owner_value}
        profile_id = f"{blueprint_id}_{owner_type.upper()}_{owner_value}"

        doc_data = {
            "profile_id": profile_id,  # Composite key
            "id": original_id,  # Original ID for readability
            "owner_type": owner_type,
            "owner_value": owner_value,
            "blueprint_id": blueprint_id,
            "slots": profile.get("slots", []),
            "metadata": profile.get("metadata", {}),
        }

        if dry_run:
            print(f"  [DRY RUN] Would create: {profile_id} (original: {original_id})")
        else:
            db.collection(collection_name).document(profile_id).set(doc_data)
            print(f"  ✓ Created: {profile_id} (original: {original_id})")

        count += 1

    return count


def main():
    parser = argparse.ArgumentParser(
        description="Migrate Prompt Design System v3 data to Firestore"
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
    tokens_path = script_dir / "tokens_library.yaml"
    blueprint_path = script_dir / "universal_blueprint.groovy"
    profiles_path = script_dir / "agent_profiles.yaml"

    # Validate files exist
    missing_files = []
    if not tokens_path.exists():
        missing_files.append(str(tokens_path))
    if not blueprint_path.exists():
        missing_files.append(str(blueprint_path))
    if not profiles_path.exists():
        missing_files.append(str(profiles_path))

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

    # Run migrations
    try:
        tokens_count = migrate_tokens(db, args.env, tokens_path, args.dry_run)
        blueprint_count = migrate_blueprint(db, args.env, blueprint_path, profiles_path, args.dry_run)
        profiles_count = migrate_agent_profiles(db, args.env, profiles_path, args.dry_run)

        print("\n" + "=" * 70)
        print("MIGRATION SUMMARY")
        print("=" * 70)
        print(f"  Environment: {args.env}")
        print(f"  Tokens migrated: {tokens_count}")
        print(f"  Blueprints migrated: {blueprint_count}")
        print(f"  Agent profiles migrated: {profiles_count}")
        print(f"  Total documents: {tokens_count + blueprint_count + profiles_count}")

        if args.dry_run:
            print("\n⚠️  This was a DRY RUN - no data was written to Firestore")
        else:
            print("\n✅ Migration completed successfully!")

    except Exception as e:
        print(f"\n❌ Migration failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

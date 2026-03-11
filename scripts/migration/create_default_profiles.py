"""
Create Default Profiles for Prompt Design System v3 Migration.

This script generates and uploads default profiles for universal_agent_v1 blueprint.

CRITICAL: Uses ONE universal blueprint (universal_agent_v1).
Profiles select which slots to use per agent type.

Usage:
    python scripts/migration/create_default_profiles.py --dry-run  # Preview only
    python scripts/migration/create_default_profiles.py --upload   # Upload to Firestore

Phase: 5.4 (Migration - Default Profile Creation)
Date: 2026-02-02
Updated: 2026-02-02 (Migrated to universal blueprint architecture)
"""

import asyncio
from typing import List, Dict
from google.cloud import firestore
import os
import sys

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from src.domain.prompt_v3.slot import OwnerType


# ============================================================================
# Default Profile Definitions
# ============================================================================

DEFAULT_PROFILES: List[Dict] = [
    # ========================================================================
    # SYSTEM Level Profiles (per agent type)
    # All use universal_agent_v1 blueprint, but select different slots
    # ========================================================================
    
    # Router Agent: Minimal personality, JSON output
    {
        "profile_id": "universal_agent_v1_SYSTEM_router",
        "blueprint_id": "universal_agent_v1",
        "owner_type": "SYSTEM",
        "owner_value": "router",
        "slots": [
            {"type": "token", "value": "COGNITIVE_PROCESS_ROUTER", "non_overridable": False},
            {"type": "token", "value": "OUTPUT_FORMAT_JSON", "non_overridable": False},
            # Exclude personality slots
            {"type": "slot", "value": "HUMOR_ENGINE", "non_overridable": True},
            {"type": "slot", "value": "ARCHETYPE", "non_overridable": True},
            {"type": "slot", "value": "VOICE", "non_overridable": True},
            {"type": "slot", "value": "RESPONSE_STYLE", "non_overridable": True},
            {"type": "slot", "value": "VIBE", "non_overridable": True},
        ],
        "description": "Router agent: JSON output, no personality"
    },
    
    # Quick Agent: Light personality, fast responses
    {
        "profile_id": "universal_agent_v1_SYSTEM_quick",
        "blueprint_id": "universal_agent_v1",
        "owner_type": "SYSTEM",
        "owner_value": "quick",
        "slots": [
            {"type": "token", "value": "COGNITIVE_PROCESS_QUICK", "non_overridable": False},
            {"type": "token", "value": "HUMOR_PRESET_LIGHT", "non_overridable": False},
            {"type": "token", "value": "VOICE_CONVERSATIONAL", "non_overridable": False},
            {"type": "token", "value": "RESPONSE_CONCISE", "non_overridable": False},
            {"type": "token", "value": "OUTPUT_FORMAT_STANDARD", "non_overridable": False},
            # ARCHETYPE, VIBE excluded (neutral personality)
            {"type": "slot", "value": "ARCHETYPE", "non_overridable": True},
            {"type": "slot", "value": "VIBE", "non_overridable": True},
        ],
        "description": "Quick agent: Light humor, conversational, concise"
    },
    
    # Smart Agent: Full personality (Ranevskaya mode)
    {
        "profile_id": "universal_agent_v1_SYSTEM_smart",
        "blueprint_id": "universal_agent_v1",
        "owner_type": "SYSTEM",
        "owner_value": "smart",
        "slots": [
            {"type": "token", "value": "COGNITIVE_PROCESS_SMART", "non_overridable": False},
            {"type": "token", "value": "HUMOR_PRESET_RANEVSKAYA", "non_overridable": False},
            {"type": "token", "value": "ARCHETYPE_INTELLECTUAL_SNIPER", "non_overridable": False},
            {"type": "token", "value": "VOICE_APHORISTIC", "non_overridable": False},
            {"type": "token", "value": "RESPONSE_CONCISE", "non_overridable": False},
            {"type": "token", "value": "VIBE_BATTLE_WEARY", "non_overridable": False},
            {"type": "token", "value": "OUTPUT_FORMAT_STANDARD", "non_overridable": False},
        ],
        "description": "Smart agent: Full personality (Ranevskaya mode)"
    },
    
    # WebSearch Agent: Data focus, weather output format
    {
        "profile_id": "universal_agent_v1_SYSTEM_websearch",
        "blueprint_id": "universal_agent_v1",
        "owner_type": "SYSTEM",
        "owner_value": "websearch",
        "slots": [
            {"type": "token", "value": "COGNITIVE_PROCESS_WEBSEARCH", "non_overridable": False},
            {"type": "token", "value": "OUTPUT_FORMAT_WEATHER", "non_overridable": False},
            # Exclude personality (focus on data)
            {"type": "slot", "value": "HUMOR_ENGINE", "non_overridable": True},
            {"type": "slot", "value": "ARCHETYPE", "non_overridable": True},
            {"type": "slot", "value": "VOICE", "non_overridable": True},
            {"type": "slot", "value": "RESPONSE_STYLE", "non_overridable": True},
            {"type": "slot", "value": "VIBE", "non_overridable": True},
        ],
        "description": "WebSearch agent: Weather output, no personality"
    },
]


async def upload_profiles_to_firestore(profiles: List[Dict], collection_name: str):
    """Upload profiles to Firestore."""
    db = firestore.Client()

    print(f"\n📤 Uploading {len(profiles)} profiles to Firestore collection: {collection_name}")

    for profile in profiles:
        doc_ref = db.collection(collection_name).document(profile["profile_id"])

        data = {
            "profile_id": profile["profile_id"],
            "blueprint_id": profile["blueprint_id"],
            "owner_type": profile["owner_type"].upper(),
            "owner_value": profile["owner_value"],
            "slots": profile["slots"],
            "created_at": firestore.SERVER_TIMESTAMP
        }

        doc_ref.set(data)
        print(f"  ✅ Uploaded: {profile['profile_id']}")

    print(f"\n✅ Upload complete! {len(profiles)} profiles in {collection_name}")


async def main():
    """Main execution."""
    import argparse

    parser = argparse.ArgumentParser(description="Create and upload default profiles for Prompt v3")
    parser.add_argument("--dry-run", action="store_true", help="Preview profiles without uploading")
    parser.add_argument("--upload", action="store_true", help="Upload profiles to Firestore")
    parser.add_argument("--collection", default="dev_agent_profiles_v3", help="Firestore collection name")

    args = parser.parse_args()

    if not args.dry_run and not args.upload:
        parser.error("Must specify either --dry-run or --upload")

    print("=" * 80)
    print("🚀 Prompt Design System v3 - Default Profile Creation")
    print("=" * 80)

    # Print summary
    print("\n" + "=" * 80)
    print("📊 Profile Summary")
    print("=" * 80)

    profiles_by_owner = {}
    for profile in DEFAULT_PROFILES:
        owner_type = profile["owner_type"]
        profiles_by_owner[owner_type] = profiles_by_owner.get(owner_type, 0) + 1

    print(f"\nTotal profiles: {len(DEFAULT_PROFILES)}")
    print("\nBy owner type:")
    for owner_type, count in sorted(profiles_by_owner.items()):
        print(f"  - {owner_type}: {count} profiles")

    print("\n" + "-" * 80)
    print("Profile Details:")
    print("-" * 80)

    for profile in DEFAULT_PROFILES:
        print(f"\n{profile['profile_id']}:")
        print(f"  Blueprint: {profile['blueprint_id']}")
        print(f"  Owner: {profile['owner_type']} / {profile['owner_value']}")
        print(f"  Slots: {len(profile['slots'])} entries")
        if profile['slots']:
            for slot in profile['slots']:
                print(
                    f"    - {slot['type']}: {slot['value']} "
                    f"(non_overridable={slot['non_overridable']})"
                )
        print(f"  Description: {profile['description']}")

    # 4-Level Resolution Example
    print("\n" + "=" * 80)
    print("🔄 4-Level Resolution Example")
    print("=" * 80)
    print("""
For user 'professional_user_example' in 'family_account_example':

Priority chain: USER > ACCOUNT > AGENT > SYSTEM

Slot: HUMOR_ENGINE
  - SYSTEM: HUMOR_PRESET_RANEVSKAYA (default)
  - AGENT: (no override)
  - ACCOUNT: HUMOR_PRESET_FAMILY_FRIENDLY (family account override)
  - USER: HUMOR_PRESET_OFF (professional mode override)
  ✅ Result: HUMOR_PRESET_OFF (USER wins)

Slot: VOICE
  - SYSTEM: VOICE_APHORISTIC (default)
  - AGENT: (no override)
  - ACCOUNT: (no override)
  - USER: VOICE_FORMAL (professional mode override)
  ✅ Result: VOICE_FORMAL (USER wins)

Slot: ARCHETYPE
  - SYSTEM: ARCHETYPE_INTELLECTUAL_SNIPER (default)
  - AGENT: (no override)
  - ACCOUNT: (no override)
  - USER: (no override)
  ✅ Result: ARCHETYPE_INTELLECTUAL_SNIPER (SYSTEM default)
""")

    # Upload if requested
    if args.upload:
        await upload_profiles_to_firestore(DEFAULT_PROFILES, args.collection)
    else:
        print(f"\n🔍 DRY RUN - No upload performed")
        print(f"   To upload, run: python {__file__} --upload --collection {args.collection}")


if __name__ == "__main__":
    asyncio.run(main())

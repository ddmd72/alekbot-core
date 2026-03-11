"""
Patch universal_agent_v1_SYSTEM_smart profile: add PROTOCOL_SMART_AGENT_SELECTION.

Token was accidentally removed during universal agent blueprint refactoring.
This script adds it back to the protocols slot without touching other fields.

Usage:
    python scripts/migration/add_protocol_smart_selection.py --dry-run   # preview
    python scripts/migration/add_protocol_smart_selection.py --upload     # apply

Collection: development_domain_prompt_profiles_v3 (default)
"""

import asyncio
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from google.cloud import firestore


PROFILE_ID = "universal_agent_v1_SYSTEM_smart"
MISSING_TOKEN = "PROTOCOL_SMART_AGENT_SELECTION"
MISSING_SLOT = {"type": "token", "value": MISSING_TOKEN, "non_overridable": False}


async def main():
    parser = argparse.ArgumentParser(description="Add PROTOCOL_SMART_AGENT_SELECTION back to smart profile")
    parser.add_argument('--dry-run', action='store_true', help='Preview without writing')
    parser.add_argument('--upload', action='store_true', help='Apply patch and upload')
    parser.add_argument('--collection', default='development_domain_prompt_profiles_v3',
                        help='Firestore collection name')
    args = parser.parse_args()

    if not args.dry_run and not args.upload:
        parser.error('Must specify either --dry-run or --upload')

    db = firestore.AsyncClient(database='us-production')
    doc_ref = db.collection(args.collection).document(PROFILE_ID)
    doc = await doc_ref.get()

    if not doc.exists:
        print(f"ERROR: profile '{PROFILE_ID}' not found in {args.collection}")
        return

    data = doc.to_dict()
    slots = data.get('slots', [])

    existing_values = [s.get('value') for s in slots if isinstance(s, dict)]
    print(f"\nProfile:    {PROFILE_ID}")
    print(f"Collection: {args.collection}")
    print(f"Current slots ({len(slots)}): {existing_values}")

    if MISSING_TOKEN in existing_values:
        print(f"\n✅ {MISSING_TOKEN} already present — nothing to do.")
        return

    patched_slots = slots + [MISSING_SLOT]
    print(f"\nAdding: {MISSING_SLOT}")
    print(f"Patched slots ({len(patched_slots)}): {[s.get('value') for s in patched_slots]}")

    if args.dry_run:
        print("\nDRY RUN — no changes written.")
        return

    await doc_ref.update({'slots': patched_slots})
    print(f"\n✅ Profile updated in {args.collection}")
    print("Next: redeploy or $admin_cache_reset to pick up the change.")


if __name__ == '__main__':
    asyncio.run(main())

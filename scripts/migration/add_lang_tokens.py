"""
Add LANG_* tokens to Firestore system token collection.

RFC: docs/10_rfcs/MULTILINGUAL_SUPPORT_RFC.md §7

Tokens created:
  LANG_MIRROR       — mirror user's input language
  LANG_FIXED_UK     — always respond in Ukrainian
  LANG_FIXED_EN     — always respond in English
  LANG_FIXED_FR     — always respond in French
  LANG_FIXED_ES     — always respond in Spanish

These tokens are fetched by PromptAssemblyService via resolve_lang_token_id()
and injected as a language_directive block in the assembled prompt.

Usage:
    python scripts/migration/add_lang_tokens.py --dry-run   # preview
    python scripts/migration/add_lang_tokens.py --upload     # apply

Collection: development_domain_prompt_tokens_v3_system (default)
"""

import asyncio
import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from google.cloud import firestore

TOKENS = [
    {
        "token_id": "LANG_MIRROR",
        "category": "output_language",
        "class": "policies",
        "content": "Respond in the same language the user writes in. Mirror their language exactly. Do not switch unless they do.",
        "metadata": {
            "version": "1.0",
            "author": "system",
            "description": "Mirror user's input language in every response",
        },
    },
    {
        "token_id": "LANG_FIXED_UK",
        "category": "output_language",
        "class": "policies",
        "content": "Always respond in Ukrainian (uk), regardless of what language the user writes in.",
        "metadata": {
            "version": "1.0",
            "author": "system",
            "description": "Fixed Ukrainian response language",
        },
    },
    {
        "token_id": "LANG_FIXED_EN",
        "category": "output_language",
        "class": "policies",
        "content": "Always respond in English, regardless of what language the user writes in.",
        "metadata": {
            "version": "1.0",
            "author": "system",
            "description": "Fixed English response language",
        },
    },
    {
        "token_id": "LANG_FIXED_FR",
        "category": "output_language",
        "class": "policies",
        "content": "Always respond in French (français), regardless of what language the user writes in.",
        "metadata": {
            "version": "1.0",
            "author": "system",
            "description": "Fixed French response language",
        },
    },
    {
        "token_id": "LANG_FIXED_ES",
        "category": "output_language",
        "class": "policies",
        "content": "Always respond in Spanish (español), regardless of what language the user writes in.",
        "metadata": {
            "version": "1.0",
            "author": "system",
            "description": "Fixed Spanish response language",
        },
    },
]


async def main():
    parser = argparse.ArgumentParser(description="Add LANG_* tokens to Firestore")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--upload", action="store_true", help="Write tokens to Firestore")
    parser.add_argument(
        "--collection",
        default="development_domain_prompt_tokens_v3_system",
        help="Firestore collection name",
    )
    parser.add_argument(
        "--database",
        default=os.environ.get("FIRESTORE_DATABASE", "us-production"),
        help="Firestore database ID",
    )
    args = parser.parse_args()

    if not args.dry_run and not args.upload:
        parser.error("Must specify either --dry-run or --upload")

    print(f"\nCollection: {args.collection}")
    print(f"Database:   {args.database}")
    print(f"Mode:       {'DRY RUN' if args.dry_run else 'UPLOAD'}")
    print(f"Tokens:     {len(TOKENS)}\n")

    if not args.dry_run:
        db = firestore.AsyncClient(database=args.database)

    for token in TOKENS:
        token_id = token["token_id"]
        print(f"  {token_id}")
        print(f"    class={token['class']}  category={token['category']}")
        print(f"    content: {token['content'][:60]}...")

        if args.upload:
            doc_ref = db.collection(args.collection).document(token_id)
            doc = await doc_ref.get()
            if doc.exists:
                print(f"    → Already exists, skipping (use --force to overwrite)")
            else:
                payload = {**token, "uploaded_by": "migration", "source_file": __file__}
                await doc_ref.set(payload)
                print(f"    → Written to Firestore")
        print()

    if args.dry_run:
        print("DRY RUN complete — no changes written.")
    else:
        print(f"\nDone. Next: $admin_cache_reset or redeploy to pick up new tokens.")


if __name__ == "__main__":
    asyncio.run(main())

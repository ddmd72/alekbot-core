"""
Patch universal_agent_v1 blueprint template: remove runtime placeholder blocks.

Changes:
  1. Remove `current_date_time { [[CURRENT_DATE_TIME]] }` block
  2. Remove `knowledge_base { biographical_context: ... conversation_history: ... }` header block
     (the second knowledge_base block with static agent content is preserved)

After this migration:
  - Blueprint is purely static (token slots + hardcoded structure)
  - Runtime injection (bio, history, datetime) is handled entirely by code
  - _inject_runtime_context() appends these blocks conditionally (no empty wrappers)

Usage:
    python scripts/migration/update_blueprint_template.py --dry-run   # preview diff
    python scripts/migration/update_blueprint_template.py --upload     # apply to Firestore

Collection: development_domain_prompt_blueprints_v3 (default) or pass --collection
"""

import asyncio
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from google.cloud import firestore


BLUEPRINT_ID = "universal_agent_v1"


def patch_template(template: str) -> str:
    """Remove runtime placeholder blocks from the blueprint template.

    Removes:
      1. current_date_time { ... [[CURRENT_DATE_TIME]] ... }  (single-block)
      2. knowledge_base { biographical_context: ... conversation_history: ... }
         — the first occurrence, which contains ONLY the runtime placeholders.
         The second knowledge_base block (static agent content) is preserved.
    """
    # ------------------------------------------------------------------ #
    # 1. Remove current_date_time block                                    #
    # Matches: optional leading whitespace + "current_date_time" + "{...}" #
    # where the block contains [[CURRENT_DATE_TIME]]                       #
    # ------------------------------------------------------------------ #
    template = re.sub(
        r'\n[ \t]*current_date_time[ \t]*\{[^}]*\[\[CURRENT_DATE_TIME\]\][^}]*\}',
        '',
        template,
        flags=re.DOTALL,
    )

    # ------------------------------------------------------------------ #
    # 2. Remove first knowledge_base block that contains ONLY runtime      #
    #    placeholders ([[BIOGRAPHICAL_CONTEXT]] and/or                     #
    #    [[CONVERSATION_HISTORY]]).                                         #
    #    We identify it by the presence of [[BIOGRAPHICAL_CONTEXT]].        #
    # ------------------------------------------------------------------ #
    marker = '[[BIOGRAPHICAL_CONTEXT]]'
    if marker in template:
        pos = template.index(marker)
        start = template.rfind('knowledge_base', 0, pos)
        if start != -1:
            # Include any leading whitespace/newline before "knowledge_base"
            block_start = start
            while block_start > 0 and template[block_start - 1] in (' ', '\t'):
                block_start -= 1
            if block_start > 0 and template[block_start - 1] == '\n':
                block_start -= 1

            # Find the matching closing brace
            brace_pos = template.index('{', start)
            depth = 0
            i = brace_pos
            while i < len(template):
                if template[i] == '{':
                    depth += 1
                elif template[i] == '}':
                    depth -= 1
                    if depth == 0:
                        block_end = i + 1
                        if block_end < len(template) and template[block_end] == '\n':
                            block_end += 1
                        template = template[:block_start] + template[block_end:]
                        break
                i += 1

    # ------------------------------------------------------------------ #
    # 3. Collapse 3+ consecutive blank lines → 2 newlines (cosmetic)      #
    # ------------------------------------------------------------------ #
    template = re.sub(r'\n{3,}', '\n\n', template)

    return template


async def main():
    parser = argparse.ArgumentParser(description="Patch universal_agent_v1 blueprint template")
    parser.add_argument('--dry-run', action='store_true', help='Show diff without uploading')
    parser.add_argument('--upload', action='store_true', help='Apply patch and upload to Firestore')
    parser.add_argument('--collection', default='development_domain_prompt_blueprints_v3',
                        help='Firestore collection name')
    args = parser.parse_args()

    if not args.dry_run and not args.upload:
        parser.error('Must specify either --dry-run or --upload')

    db = firestore.AsyncClient(database='us-production')
    doc_ref = db.collection(args.collection).document(BLUEPRINT_ID)
    doc = await doc_ref.get()

    if not doc.exists:
        print(f"ERROR: blueprint '{BLUEPRINT_ID}' not found in {args.collection}")
        return

    data = doc.to_dict()
    original = data.get('template', '')

    patched = patch_template(original)

    # Show diff summary
    orig_lines = original.splitlines()
    patched_lines = patched.splitlines()
    print(f"\nBlueprint: {BLUEPRINT_ID}")
    print(f"Collection: {args.collection}")
    print(f"Original: {len(original)} chars / {len(orig_lines)} lines")
    print(f"Patched:  {len(patched)} chars / {len(patched_lines)} lines")
    print(f"Removed:  {len(original) - len(patched)} chars / {len(orig_lines) - len(patched_lines)} lines")

    removed_markers = []
    if '[[CURRENT_DATE_TIME]]' in original and '[[CURRENT_DATE_TIME]]' not in patched:
        removed_markers.append('[[CURRENT_DATE_TIME]]')
    if '[[BIOGRAPHICAL_CONTEXT]]' in original and '[[BIOGRAPHICAL_CONTEXT]]' not in patched:
        removed_markers.append('[[BIOGRAPHICAL_CONTEXT]]')
    if '[[CONVERSATION_HISTORY]]' in original and '[[CONVERSATION_HISTORY]]' not in patched:
        removed_markers.append('[[CONVERSATION_HISTORY]]')

    if removed_markers:
        print(f"\nRemoved placeholders: {', '.join(removed_markers)}")
    else:
        print("\nWARNING: No placeholders found/removed — template may already be patched or structure differs")

    remaining = re.findall(r'\[\[[A-Z_]+\]\]', patched)
    if remaining:
        print(f"WARNING: Remaining [[...]] placeholders after patch: {remaining}")

    if args.dry_run:
        print("\n--- PATCHED TEMPLATE (first 60 lines) ---")
        for line in patched_lines[:60]:
            print(line)
        if len(patched_lines) > 60:
            print(f"... ({len(patched_lines) - 60} more lines)")
        print("\nDRY RUN — no changes written.")
        return

    await doc_ref.update({'template': patched})
    print(f"\n✅ Blueprint updated in {args.collection}")
    print("Next: invalidate PromptAssemblyService cache ($admin_cache_reset or redeploy)")


if __name__ == '__main__':
    asyncio.run(main())

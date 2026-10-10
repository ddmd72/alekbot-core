"""
Keep file operations out of long-term memory (docs/10_rfcs/USER_DRIVE_RFC.md §4.12).

Appends one line to ``exclude: [`` in ``rule Trivial_Exclusions()`` of the
``CONSOLIDATION_TAXONOMY`` system token. The edit is anchored on the exact text of the
list's last element followed by its closing ``]`` and aborts unless that anchor occurs
exactly once, so a re-worded token is never half-patched. Leading whitespace is taken from
the live token, not assumed: the token's indentation cannot be verified from the repo (the
RFC copy and the plan disagree by four spaces), and a wrong guess would only turn into a
spurious "re-worded" abort.

    python scripts/prompt/migrate_file_ops_exclusion_token.py --dry-run
    python scripts/prompt/migrate_file_ops_exclusion_token.py --apply
    python scripts/prompt/migrate_file_ops_exclusion_token.py --revert <backup.json>

Backups land in scripts/memory/ (gitignored — live content).
The prompt cache holds 24h; a change is visible to new sessions after it expires.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from google.cloud import firestore  # noqa: E402

from src.config.environment import EnvironmentConfig  # noqa: E402

_BACKUP_DIR = Path(__file__).resolve().parents[1] / "memory"
_TOKEN_ID = "CONSOLIDATION_TAXONOMY"

# The last element of `exclude: [` today, and the line to append after it (RFC §4.12).
_LAST_ITEM = '''"Temporary debugging state: 'Testing feature X' (unless ongoing project)"'''
_NEW_ITEM = ('''"File operations: saving, opening, moving, renaming, deleting files or folders on '''
             '''the user's drive or in chat — the drive is the record of what exists and where"''')

# Exact element text, then the list's closing bracket on the next line. Indentation is
# captured from the live token (group 1 = the item's, group 2 = the bracket's).
_ANCHOR_RE = re.compile(
    r"^([ \t]*)" + re.escape(_LAST_ITEM) + r"[ \t]*\n([ \t]*)\]",
    re.M,
)


def patch(content: str) -> tuple[str, str, str] | None:
    """(new_content, old_block, new_block) — or None when the anchor is not found exactly once."""
    matches = list(_ANCHOR_RE.finditer(content))
    if len(matches) != 1:
        return None
    m = matches[0]
    item_indent, bracket_indent = m.group(1), m.group(2)
    old_block = m.group(0)
    new_block = f"{item_indent}{_LAST_ITEM},\n{item_indent}{_NEW_ITEM}\n{bracket_indent}]"
    return content[: m.start()] + new_block + content[m.end():], old_block, new_block


async def _run(mode: str, backup_path: str | None) -> int:
    env = EnvironmentConfig()
    db = firestore.AsyncClient(database=os.environ.get("FIRESTORE_DATABASE", "us-production"))
    collection = f"{env.domain_prompt_tokens_collection}_system"

    if mode == "revert":
        payload = json.loads(Path(backup_path).read_text())
        if payload.get("collection") != collection:
            print(f"ERROR backup is for {payload.get('collection')!r}, this environment uses {collection!r}")
            return 1
        for token_id, content in payload["tokens"].items():
            await db.collection(collection).document(token_id).update({"content": content})
            print(f"restored {token_id}")
        return 0

    doc = await db.collection(collection).document(_TOKEN_ID).get()
    if not doc.exists:
        print(f"ERROR {_TOKEN_ID}: not found in {collection}")
        return 1
    content = (doc.to_dict() or {}).get("content", "")

    if _NEW_ITEM in content:
        print(f"OK {_TOKEN_ID}: already up to date")
        return 0
    patched = patch(content)
    if patched is None:
        n = len(_ANCHOR_RE.findall(content))
        hint = ("the element text is present but not followed by the closing ']' on the next line"
                if _LAST_ITEM in content else "the element text is absent")
        print(f"ERROR {_TOKEN_ID}: anchor occurs {n} times (need exactly 1) — {hint}; "
              f"token was re-worded, patch by hand:\n{_LAST_ITEM}")
        return 1
    new_content, old_block, new_block = patched
    print(f"OK {_TOKEN_ID}: 1/1 edit applied, {len(new_content) - len(content):+d} chars")

    if mode == "dry-run":
        for line in difflib.unified_diff(
            old_block.splitlines(), new_block.splitlines(), "old", "new", lineterm="", n=0
        ):
            print(line)
        print("\n(dry run — nothing written)")
        return 0

    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = _BACKUP_DIR / f"prompt_tokens_backup_{stamp}.json"
    backup.write_text(json.dumps({"collection": collection, "tokens": {_TOKEN_ID: content}}, indent=2))
    print(f"\nbackup -> {backup}")

    await db.collection(collection).document(_TOKEN_ID).update(
        {"content": new_content, "updated_at": datetime.now(timezone.utc)}
    )
    print(f"wrote {_TOKEN_ID}")
    print("\nPrompt cache TTL is 24h — new sessions pick this up as it expires.")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--apply", action="store_true")
    group.add_argument("--revert", metavar="BACKUP_JSON")
    args = ap.parse_args()

    mode = "revert" if args.revert else ("apply" if args.apply else "dry-run")
    sys.exit(asyncio.run(_run(mode, args.revert)))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Deploy step of fact profile PR 13 (FACT_PROFILE_RFC §9, OD-6): mark the standing directives with the `directive` flag.

From PR 13 a fact is a standing directive when it carries `directive: true`; the consolidator writes the flag, and every
read of directives (the prompt's rulebook, Stage 2b, the cap) filters on it. A fact written before PR 13 carries the
consolidator's old `domain` instead, and only that marks it. Per account, this script sets `directive: true` on every fact
document (all states and versions) whose old `domain` is `agent_directive` (any letter case) and whose `directive` is not
already true; it writes that one field and nothing else. Run it before the deploy and once right after (the old code
may still write a directive in between): a second run marks only what the first did not.

The accounts are named by `.env` keys (default `DEV_ACCOUNT_ID`; `--account-key` repeats for each live account), never by
an id on the command line. Dry run by default (reads only): per account, how many documents it would mark and how many
already carry the flag. `--apply` asks to type the number of documents it will mark, then writes in batches.
stdout prints the `.env` key names and counts only.

Usage:
    python -m scripts.migration.fact_directive_flag_mark [--account-key KEY ...] [--apply]
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

from google.cloud.firestore_v1.base_query import FieldFilter  # noqa: E402

OLD_DIRECTIVE_DOMAIN = "agent_directive"
FLAG = "directive"
BATCH_SIZE = 400                   # below Firestore's 500 writes per batch
DEFAULT_ACCOUNT_KEYS = ("DEV_ACCOUNT_ID",)

CANDIDATES = "to_mark"
ALREADY = "already_marked"
MARKED = "marked"


def _old_directive(data: Mapping[str, Any]) -> bool:
    domain = data.get("domain")
    return isinstance(domain, str) and domain.lower() == OLD_DIRECTIVE_DOMAIN


async def scan(db: Any, collection: str, account_id: str) -> Tuple[List[Any], int]:
    """The references of the account's old-directive documents without the flag, and how many already carry it."""
    query = db.collection(collection).where(filter=FieldFilter("account_id", "==", account_id)).select(["domain", FLAG])
    to_mark: List[Any] = []
    already = 0
    async for snap in query.stream():
        data = snap.to_dict() or {}
        if not _old_directive(data):
            continue
        if data.get(FLAG) is True:
            already += 1
        else:
            to_mark.append(snap.reference)
    return to_mark, already


async def mark(db: Any, refs: Sequence[Any]) -> int:
    """Set the flag on `refs`, `BATCH_SIZE` writes per commit. Returns how many were written."""
    written = 0
    for start in range(0, len(refs), BATCH_SIZE):
        batch = db.batch()
        chunk = refs[start:start + BATCH_SIZE]
        for ref in chunk:
            batch.update(ref, {FLAG: True})
        await batch.commit()
        written += len(chunk)
    return written


def confirm_count(count: int) -> bool:
    """Human-only gate: type the number of documents to mark. A non-interactive shell aborts."""
    try:
        answer = input(f"Type {count} to set `{FLAG}: true` on {count} fact documents (anything else aborts): ")
    except EOFError:
        print("non-interactive shell: --apply aborted (no write)")
        return False
    return answer.strip() == str(count)


async def run(
    db: Any, collection: str, accounts: Mapping[str, str], apply: bool,
    confirm: Callable[[int], bool] = confirm_count,
) -> Dict[str, Dict[str, int]]:
    """Scan every account (label → account id), print the counts, and with `apply` (after `confirm`) mark. Returns the
    counts it printed, per label."""
    scanned = {label: await scan(db, collection, account_id) for label, account_id in accounts.items()}
    counts = {label: {CANDIDATES: len(refs), ALREADY: already, MARKED: 0} for label, (refs, already) in scanned.items()}
    total = sum(c[CANDIDATES] for c in counts.values())
    print(f"Mode: {'APPLY' if apply else 'DRY-RUN'}")
    if apply and total and confirm(total):
        for label, (refs, _) in scanned.items():
            counts[label][MARKED] = await mark(db, refs)
    elif apply and total:
        print("not confirmed: nothing written")
    for label, values in counts.items():
        print(f"{label}: " + ", ".join(f"{name} {value}" for name, value in values.items()))
    return counts


def account_ids(keys: Sequence[str], env: Mapping[str, str]) -> Dict[str, str]:
    """`.env` key → account id; a key with no value raises SystemExit naming the key."""
    missing = [key for key in keys if not env.get(key)]
    if missing:
        raise SystemExit(f"no account id under: {', '.join(missing)}")
    return {key: env[key] for key in keys}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account-key", action="append", dest="account_keys",
                        help="the .env key holding one account id (repeat for each account; default DEV_ACCOUNT_ID)")
    parser.add_argument("--apply", action="store_true", help="write the flag (default: dry run)")
    args = parser.parse_args(argv)
    from scripts._firestore import async_client   # loads .env at import: only when run for real
    from src.config.environment import EnvironmentConfig
    accounts = account_ids(args.account_keys or DEFAULT_ACCOUNT_KEYS, os.environ)
    collection = EnvironmentConfig().domain_facts_collection

    async def go() -> None:   # the client is built inside the loop it runs on
        await run(async_client(), collection, accounts, args.apply)

    asyncio.run(go())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

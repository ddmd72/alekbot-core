"""Read-only counts of the stored mail by v2 `card_state` (email index v2 slice 6; the owner's check after deploy and
slice 7's input).

    python scripts/email/card_store_status.py [--since YYYY-MM-DD]
    python scripts/email/card_store_status.py --scans [--ids-out scripts/memory/email_cards/scans.txt]

`DEV_USER_ID` comes from `.env`. Prints counts only: never ids, subjects or card content. `--since` limits the count to
mail indexed on or after that date (UTC).

`--scans` (read only, slice 5) selects the carded or empty mail with at least one attachment stored `scan_not_read`: it
prints their count by card state and the projected reader dollars (an assumed cost per scanned mail). The ids go only to
`--ids-out`, which must be under `scripts/memory/` (gitignored); nothing is written to the store."""
import argparse
import asyncio
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.cloud.firestore_v1.base_query import FieldFilter  # noqa: E402

from _firestore import async_client  # noqa: E402
from src.config.environment import EnvironmentConfig  # noqa: E402
from src.domain.drive_index_params import READER_PAGE_USD_ESTIMATE  # noqa: E402

STATES = ("carded", "empty", "pending", "abandoned", "source_gone")
SCAN_STATES = ("carded", "empty")
SCAN_MAIL_USD_ESTIMATE = 3 * READER_PAGE_USD_ESTIMATE   # assumed: a 3-page scan
REPO = Path(__file__).resolve().parent.parent.parent


def scan_selection(rows: Iterable[Tuple[str, Mapping[str, Any]]]) -> Tuple[List[str], "Counter[str]"]:
    """(email ids, count by card state) of the rows whose card has an attachment stored `scan_not_read`. A row is
    (email id, the stored document's fields): `card_state` and `card.attachments` are read; only carded or empty
    mail is selected (a pending one has no settled attachments)."""
    ids: List[str] = []
    states: "Counter[str]" = Counter()
    for email_id, data in rows:
        state = data.get("card_state")
        if state not in SCAN_STATES:
            continue
        attachments = (data.get("card") or {}).get("attachments") or []
        if any(isinstance(a, Mapping) and a.get("read") == "scan_not_read" for a in attachments):
            ids.append(email_id)
            states[state] += 1
    return ids, states


def under_scripts_memory(path: Path) -> bool:
    try:
        path.resolve().relative_to((REPO / "scripts" / "memory").resolve())
    except ValueError:
        return False
    return True


async def _scan_mode(col, user_id: str, ids_out: str) -> None:
    out = Path(ids_out) if ids_out else None
    if out is not None and not under_scripts_memory(out):
        sys.exit("--ids-out must be under scripts/memory/")
    query = (col.where(filter=FieldFilter("user_id", "==", user_id))
             .where(filter=FieldFilter("card_state", "in", list(SCAN_STATES)))
             .select(["card_state", "card.attachments"]))
    prefix = f"{user_id}_"
    rows = []
    async for snap in query.stream():
        rows.append((snap.id[len(prefix):] if snap.id.startswith(prefix) else snap.id, snap.to_dict() or {}))
    ids, states = scan_selection(rows)
    print(f"mails with a scan_not_read attachment: {len(ids)}")
    for state in SCAN_STATES:
        print(f"card_state {state:<12} {states.get(state, 0)}")
    print(f"projected reader cost: ${len(ids) * SCAN_MAIL_USD_ESTIMATE:.2f} (at ${SCAN_MAIL_USD_ESTIMATE:.4f} a scanned mail)")
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("".join(f"{i}\n" for i in ids), encoding="utf-8")
        print(f"ids written to {out}")


async def _count(query) -> int:
    rows = await query.count().get()
    return int(rows[0][0].value)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="YYYY-MM-DD: only mail indexed on or after this date")
    parser.add_argument("--scans", action="store_true", help="select the mail whose scans were not read")
    parser.add_argument("--ids-out", default="", help="with --scans: one id per line, under scripts/memory/")
    args = parser.parse_args()
    user_id = os.getenv("DEV_USER_ID")
    if not user_id:
        sys.exit("DEV_USER_ID is not set (.env)")
    col = async_client().collection(EnvironmentConfig().domain_email_facts_collection)
    if args.scans:
        await _scan_mode(col, user_id, args.ids_out)
        return

    def scope(extra=None):
        query = col.where(filter=FieldFilter("user_id", "==", user_id))
        if args.since:
            since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            query = query.where(filter=FieldFilter("indexed_at", ">=", since))
        return query.where(filter=FieldFilter(*extra)) if extra else query

    total = await _count(scope())
    per_state = {s: await _count(scope(("card_state", "==", s))) for s in STATES}
    pending_vectors = await _count(scope(("card_vector_pending", "==", True)))
    print(f"all documents:        {total}")
    for state, n in per_state.items():
        print(f"card_state {state:<12} {n}")
    print(f"no card fields:       {total - sum(per_state.values())}")
    print(f"card_vector_pending:  {pending_vectors}")


if __name__ == "__main__":
    asyncio.run(main())

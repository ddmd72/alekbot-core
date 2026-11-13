"""Read-only counts of the stored mail by v2 `card_state` (email index v2 slice 6; the owner's check after deploy and
slice 7's input).

    python scripts/email/card_store_status.py [--since YYYY-MM-DD]

`DEV_USER_ID` comes from `.env`. Prints counts only: never ids, subjects or card content. `--since` limits the count to
mail indexed on or after that date (UTC)."""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.cloud.firestore_v1.base_query import FieldFilter  # noqa: E402

from _firestore import async_client  # noqa: E402
from src.config.environment import EnvironmentConfig  # noqa: E402

STATES = ("carded", "empty", "pending", "abandoned", "source_gone")


async def _count(query) -> int:
    rows = await query.count().get()
    return int(rows[0][0].value)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="YYYY-MM-DD: only mail indexed on or after this date")
    args = parser.parse_args()
    user_id = os.getenv("DEV_USER_ID")
    if not user_id:
        sys.exit("DEV_USER_ID is not set (.env)")
    col = async_client().collection(EnvironmentConfig().domain_email_facts_collection)

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

"""Storage cleanup after email index v2, slice 10: delete the v1 email vector fields from every stored mail document.

    python scripts/email/drop_v1_vectors.py            # dry run: counts only, writes nothing
    python scripts/email/drop_v1_vectors.py --live     # deletes the fields

The five fields are `vector`, `tags_vector`, `metadata_vector`, `attachments_vector` and `embedding_pending`. Nothing
else is touched: the v1 text, tags, category and metadata, the card fields (`card_vector` included) and
`consolidated_at` stay as they are. The code already ignores the fields (a stored document with them still parses), so
this frees storage only; it is safe to skip or to run twice. Prints counts only, never ids or content.
"""
import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

V1_FIELDS = ("vector", "tags_vector", "metadata_vector", "attachments_vector", "embedding_pending")
BATCH_SIZE = 500


async def drop_v1_vectors(db: Any, col: Any, delete_field: Any, *, live: bool) -> Dict[str, int]:
    """Walk the facts collection `col` of `db`; with `live`, delete `V1_FIELDS` from each document that carries any
    of them, in batches of 500. Returns counts: documents seen, documents that carry a v1 field, documents updated."""
    counts = {"scanned": 0, "with_v1_fields": 0, "updated": 0}
    batch, pending = db.batch(), 0
    async for doc in col.select(list(V1_FIELDS)).stream():
        counts["scanned"] += 1
        present = set((doc.to_dict() or {}).keys()) & set(V1_FIELDS)
        if not present:
            continue
        counts["with_v1_fields"] += 1
        if not live:
            continue
        batch.update(doc.reference, {name: delete_field for name in present})
        pending += 1
        if pending >= BATCH_SIZE:
            await batch.commit()
            counts["updated"] += pending
            batch, pending = db.batch(), 0
    if live and pending:
        await batch.commit()
        counts["updated"] += pending
    return counts


def format_report(counts: Dict[str, int], *, live: bool) -> str:
    lines = [f"documents scanned:        {counts['scanned']}",
             f"documents with v1 fields: {counts['with_v1_fields']}"]
    lines.append(f"documents updated:        {counts['updated']}" if live
                 else "dry run: nothing written (pass --live to delete the fields)")
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="delete the fields (default: dry run)")
    args = parser.parse_args()
    from google.cloud.firestore import DELETE_FIELD  # noqa: E402

    from _firestore import async_client  # noqa: E402
    from src.config.environment import EnvironmentConfig  # noqa: E402

    db = async_client()
    col = db.collection(EnvironmentConfig().domain_email_facts_collection)
    print(format_report(await drop_v1_vectors(db, col, DELETE_FIELD, live=args.live), live=args.live))


if __name__ == "__main__":
    asyncio.run(main())

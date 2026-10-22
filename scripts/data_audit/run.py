"""Read-only data audit: stream one entity's collection, check every stored document, write a JSON report.

    python -m scripts.data_audit.run facts [--account <account_id>] [--limit N]

- Reads only (a streamed collection query); the module issues no write of any kind.
- The report goes to `scripts/memory/data_audit_<entity>_<UTC time>.json` (gitignored): document counts per violated
  class plus the offending document ids. It holds no text, vector or other field values.
- stdout prints counts only, never ids.
- `--account` reads one account's documents (a legacy owner_id-only document has no `account_id` and is not seen).
  `--limit` reads the first N documents; the lineage classes are unreliable on a partial read.
- Entities: only `facts` today. Another entity joins by adding a `CHECKERS` entry when its checker exists.
"""
import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from google.cloud.firestore_v1.base_query import FieldFilter

from scripts.data_audit.facts import check_fact, check_lineages

MEMORY_DIR = Path(__file__).resolve().parents[1] / "memory"

# entity -> EnvironmentConfig property naming its collection; per-document check; cross-document check over the
# `keep` fields only (a full document would hold its vectors in memory for the whole run).
CHECKERS: dict[str, dict[str, Any]] = {
    "facts": {
        "collection": "domain_facts_collection",
        "per_doc": check_fact,
        "across": check_lineages,
        "keep": ("lineage_id", "is_current"),
    },
}


async def audit(db: Any, entity: str, collection: str, account_id: Optional[str] = None,
                limit: Optional[int] = None) -> dict[str, Any]:
    """The report of one entity: document count and, per violated class, the ids of the offending documents."""
    spec = CHECKERS[entity]
    query: Any = db.collection(collection)
    if account_id:
        query = query.where(filter=FieldFilter("account_id", "==", account_id))
    if limit:
        query = query.limit(limit)
    classes: dict[str, list[str]] = {}
    kept: list[tuple[str, dict[str, Any]]] = []
    count = 0
    async for snap in query.stream():
        data = snap.to_dict() or {}
        count += 1
        for cls in spec["per_doc"](snap.id, data):
            classes.setdefault(cls, []).append(snap.id)
        kept.append((snap.id, {k: data.get(k) for k in spec["keep"]}))
    for cls, ids in spec["across"](kept).items():
        classes.setdefault(cls, []).extend(ids)
    return {
        "entity": entity,
        "collection": collection,
        "account_filtered": bool(account_id),
        "limit": limit,
        "documents": count,
        "classes": {cls: {"count": len(ids), "doc_ids": ids} for cls, ids in sorted(classes.items())},
    }


async def run(db: Any, entity: str, collection: str, out_dir: Path, account_id: Optional[str] = None,
              limit: Optional[int] = None, now: Optional[datetime] = None) -> Path:
    """Audit, print the counts, write the report under `out_dir` and return its path."""
    now = now or datetime.now(timezone.utc)
    report = await audit(db, entity, collection, account_id, limit)
    report["generated_at"] = now.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"data_audit_{entity}_{now:%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"{entity}: {report['documents']} documents")
    for cls, found in report["classes"].items():
        print(f"  {cls}: {found['count']}")
    print(f"report: {path}")
    return path


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only data audit.")
    parser.add_argument("entity", choices=sorted(CHECKERS))
    parser.add_argument("--account", help="read only this account's documents")
    parser.add_argument("--limit", type=int, help="read at most N documents")
    args = parser.parse_args(argv)
    from scripts._firestore import async_client   # loads .env at import: only when run for real
    from src.config.environment import EnvironmentConfig
    collection = getattr(EnvironmentConfig(), CHECKERS[args.entity]["collection"])

    async def go() -> None:   # the client is built inside the loop it runs on
        await run(async_client(), args.entity, collection, MEMORY_DIR, args.account, args.limit)

    asyncio.run(go())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

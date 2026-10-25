"""Read-only data audit: stream one entity's collection, check every stored document, write a JSON report.

    python -m scripts.data_audit.run facts [--account <account_id>] [--limit N] [--s2-cutoff <ISO time>]

- Reads only (a streamed collection query); the module issues no write of any kind.
- The report goes to `scripts/memory/data_audit_<entity>_<UTC time>.json` (gitignored): document counts per violated
  class plus the offending document ids. It holds no text, vector or other field values.
- stdout prints counts only, never ids.
- `--account` reads one account's documents (a legacy owner_id-only document has no `account_id` and is not seen).
  `--limit` reads the first N documents; the lineage and version-chain classes are unreliable on a partial read.
- `--s2-cutoff` is the time the version-chain code went live (a naive time reads as UTC). A superseded document with no
  `superseded_by` last written before it is counted under `counted`, not reported as a violation; with no cutoff
  every such document is counted, and when version pointers exist the report and stdout carry a warning that the
  post-S2 class was not judged.
- Entities: only `facts` today. Another entity joins by adding a `CHECKERS` entry when its checker exists.
"""
import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from google.cloud.firestore_v1.base_query import FieldFilter

from scripts.data_audit.facts import COUNTED_CLASSES, check_across, check_fact, s2_data_present

MEMORY_DIR = Path(__file__).resolve().parents[1] / "memory"

# entity -> EnvironmentConfig property naming its collection; per-document check; cross-document check over the
# `keep` fields only (a full document would hold its vectors in memory for the whole run), called with the S2 cutoff;
# the classes it reports that are counted, not violations; whether the kept fields show data a cutoff is needed for.
CHECKERS: dict[str, dict[str, Any]] = {
    "facts": {
        "collection": "domain_facts_collection",
        "per_doc": check_fact,
        "across": check_across,
        "counted": COUNTED_CLASSES,
        "needs_cutoff": s2_data_present,
        "keep": ("lineage_id", "is_current", "account_id", "state", "superseded_by", "replaces_fact_id", "version",
                 "last_updated"),
    },
}


async def audit(db: Any, entity: str, collection: str, account_id: Optional[str] = None,
                limit: Optional[int] = None, s2_cutoff: Optional[datetime] = None) -> dict[str, Any]:
    """The report of one entity: document count and, per violated or counted class, the ids of the documents."""
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
    for cls, ids in spec["across"](kept, s2_cutoff).items():
        classes.setdefault(cls, []).extend(ids)
    counted = {cls: ids for cls, ids in classes.items() if cls in spec["counted"]}
    warnings = []
    if s2_cutoff is None and spec["needs_cutoff"](kept):
        warnings.append("superseded_without_successor not judged: version pointers exist, pass --s2-cutoff")
    return {
        "entity": entity,
        "collection": collection,
        "account_filtered": bool(account_id),
        "limit": limit,
        "s2_cutoff": s2_cutoff.isoformat() if s2_cutoff else None,
        "documents": count,
        "classes": {cls: _found(ids) for cls, ids in sorted(classes.items()) if cls not in counted},
        "counted": {cls: _found(ids) for cls, ids in sorted(counted.items())},
        "warnings": warnings,
    }


def _found(ids: list[str]) -> dict[str, Any]:
    return {"count": len(ids), "doc_ids": ids}


async def run(db: Any, entity: str, collection: str, out_dir: Path, account_id: Optional[str] = None,
              limit: Optional[int] = None, now: Optional[datetime] = None, s2_cutoff: Optional[datetime] = None) -> Path:
    """Audit, print the counts, write the report under `out_dir` and return its path."""
    now = now or datetime.now(timezone.utc)
    report = await audit(db, entity, collection, account_id, limit, s2_cutoff)
    report["generated_at"] = now.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"data_audit_{entity}_{now:%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"{entity}: {report['documents']} documents")
    for cls, found in report["classes"].items():
        print(f"  {cls}: {found['count']}")
    for cls, found in report["counted"].items():
        print(f"  {cls}: {found['count']} (counted, not a violation)")
    for warning in report["warnings"]:
        print(f"warning: {warning}")
    print(f"report: {path}")
    return path


def parse_cutoff(text: str) -> datetime:
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO 8601 time: {text!r}") from None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only data audit.")
    parser.add_argument("entity", choices=sorted(CHECKERS))
    parser.add_argument("--account", help="read only this account's documents")
    parser.add_argument("--limit", type=int, help="read at most N documents")
    parser.add_argument("--s2-cutoff", type=parse_cutoff, help="time the version chain went live (ISO 8601; naive = UTC)")
    args = parser.parse_args(argv)
    from scripts._firestore import async_client   # loads .env at import: only when run for real
    from src.config.environment import EnvironmentConfig
    collection = getattr(EnvironmentConfig(), CHECKERS[args.entity]["collection"])

    async def go() -> None:   # the client is built inside the loop it runs on
        await run(async_client(), args.entity, collection, MEMORY_DIR, args.account, args.limit,
                  s2_cutoff=args.s2_cutoff)

    asyncio.run(go())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

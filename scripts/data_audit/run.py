"""Read-only data audit: stream one entity's collection, check every stored document, write a JSON report.

    python -m scripts.data_audit.run facts [--account <account_id>] [--limit N] [--s2-cutoff <ISO time>]
        [--expiry-required] [--janitor-ran-at <ISO time>]

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
- `--expiry-required` is for after S5's classify is applied: a current fact without `expiry_kind` is then a violation;
  without the flag it is counted, not a violation.
- `--janitor-ran-at` is for while the janitor writes: the time of that day's run. It also judges current facts past
  `expires_at` that no open review question holds (the questions collection is read, only for this flag).
- Entities: only `facts` today. Another entity joins by adding a `CHECKERS` entry when its checker exists.
"""
import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from google.cloud.firestore_v1.base_query import FieldFilter

from scripts.data_audit.facts import (
    COUNTED_CLASSES,
    EXPIRY_REQUIRED_CLASS,
    check_across,
    check_expiry,
    check_fact,
    s2_data_present,
)
from src.domain.fact_review_question import OPEN_STATUSES

MEMORY_DIR = Path(__file__).resolve().parents[1] / "memory"

# entity -> EnvironmentConfig property naming its collection; per-document check; cross-document check over the
# `keep` fields only (a full document would hold its vectors in memory for the whole run), called with the S2 cutoff;
# the classes it reports that are counted, not violations; whether the kept fields show data a cutoff is needed for.
CHECKERS: dict[str, dict[str, Any]] = {
    "facts": {
        "collection": "domain_facts_collection",
        "per_doc": check_fact,
        "expiry": check_expiry,
        "questions": "fact_review_questions_collection",
        "across": check_across,
        "counted": COUNTED_CLASSES,
        "needs_cutoff": s2_data_present,
        "keep": ("lineage_id", "is_current", "account_id", "state", "superseded_by", "replaces_fact_id", "version",
                 "last_updated"),
    },
}


async def audit(db: Any, entity: str, collection: str, account_id: Optional[str] = None,
                limit: Optional[int] = None, s2_cutoff: Optional[datetime] = None, *,
                expiry_required: bool = False, janitor_ran_at: Optional[datetime] = None,
                questions_collection: Optional[str] = None) -> dict[str, Any]:
    """The report of one entity: document count and, per violated or counted class, the ids of the documents."""
    spec = CHECKERS[entity]
    if janitor_ran_at is not None and not questions_collection:
        raise ValueError("janitor_ran_at needs questions_collection: the open questions decide which facts are waiting")
    open_ids = await _open_fact_ids(db, questions_collection) if janitor_ran_at is not None else frozenset()
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
        found = list(spec["per_doc"](snap.id, data))
        if "expiry" in spec:
            found.extend(spec["expiry"](snap.id, data, due_as_of=janitor_ran_at, open_fact_ids=open_ids))
        for cls in found:
            classes.setdefault(cls, []).append(snap.id)
        kept.append((snap.id, {k: data.get(k) for k in spec["keep"]}))
    for cls, ids in spec["across"](kept, s2_cutoff).items():
        classes.setdefault(cls, []).extend(ids)
    counted_classes = spec["counted"] | (frozenset() if expiry_required else frozenset({EXPIRY_REQUIRED_CLASS}))
    counted = {cls: ids for cls, ids in classes.items() if cls in counted_classes}
    warnings = []
    if s2_cutoff is None and spec["needs_cutoff"](kept):
        warnings.append("superseded_without_successor not judged: version pointers exist, pass --s2-cutoff")
    if janitor_ran_at is not None:
        warnings.append("current_past_expiry_not_waiting also lists facts that run deferred (pool cap) or held back "
                        "(breaker): compare with its deferred count and breaker")
    return {
        "entity": entity,
        "collection": collection,
        "account_filtered": bool(account_id),
        "limit": limit,
        "s2_cutoff": s2_cutoff.isoformat() if s2_cutoff else None,
        "expiry_required": expiry_required,
        "janitor_ran_at": janitor_ran_at.isoformat() if janitor_ran_at else None,
        "documents": count,
        "classes": {cls: _found(ids) for cls, ids in sorted(classes.items()) if cls not in counted},
        "counted": {cls: _found(ids) for cls, ids in sorted(counted.items())},
        "warnings": warnings,
    }


async def _open_fact_ids(db: Any, collection: Optional[str]) -> frozenset[str]:
    """The `fact_id` of every question the review still holds."""
    query = db.collection(collection).where(
        filter=FieldFilter("status", "in", [s.value for s in OPEN_STATUSES])).select(["fact_id"])
    return frozenset([(snap.to_dict() or {}).get("fact_id") async for snap in query.stream()])


def _found(ids: list[str]) -> dict[str, Any]:
    return {"count": len(ids), "doc_ids": ids}


async def run(db: Any, entity: str, collection: str, out_dir: Path, account_id: Optional[str] = None,
              limit: Optional[int] = None, now: Optional[datetime] = None, s2_cutoff: Optional[datetime] = None, *,
              expiry_required: bool = False, janitor_ran_at: Optional[datetime] = None,
              questions_collection: Optional[str] = None) -> Path:
    """Audit, print the counts, write the report under `out_dir` and return its path."""
    now = now or datetime.now(timezone.utc)
    report = await audit(db, entity, collection, account_id, limit, s2_cutoff, expiry_required=expiry_required,
                         janitor_ran_at=janitor_ran_at, questions_collection=questions_collection)
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
    parser.add_argument("--expiry-required", action="store_true",
                        help="after S5's classify is applied: a current fact without expiry_kind is a violation; "
                             "without the flag it is counted")
    parser.add_argument("--janitor-ran-at", type=parse_cutoff,
                        help="while the janitor writes: the time of that day's run; judges current facts past "
                             "expires_at that no open question holds")
    args = parser.parse_args(argv)
    from scripts._firestore import async_client   # loads .env at import: only when run for real
    from src.config.environment import EnvironmentConfig
    env = EnvironmentConfig()
    collection = getattr(env, CHECKERS[args.entity]["collection"])
    questions = getattr(env, CHECKERS[args.entity]["questions"]) if args.janitor_ran_at else None

    async def go() -> None:   # the client is built inside the loop it runs on
        await run(async_client(), args.entity, collection, MEMORY_DIR, args.account, args.limit,
                  s2_cutoff=args.s2_cutoff, expiry_required=args.expiry_required,
                  janitor_ran_at=args.janitor_ran_at, questions_collection=questions)

    asyncio.run(go())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

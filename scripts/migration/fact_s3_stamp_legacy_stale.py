#!/usr/bin/env python3
"""
Deploy step of Facts S3 (owner Q6 (a)): stamp `stale_since` on the legacy stale facts that have none.

A fact with `state == "stale"` and no `stale_since` predates the S3 janitor. Stamping it puts it in S4's review queue
(oldest first) and keeps the janitor's backlog age known. Per document the script writes `stale_since = now` and one
`lifecycle_events` entry {from: stale, to: stale, actor: migration}, nothing else: `last_updated` stays, because it is
the only age signal these facts have; state, text and vectors are never touched.

Dry run by default (reads only). `--apply` writes each document under the `update_time` of the scanned snapshot; a
document changed since the scan is skipped and counted (re-run to pick it up). A second `--apply` stamps nothing.

stdout prints counts only. Ids, account prefixes, `last_updated` and the fact text go to the gitignored
`scripts/memory/fact_s3_legacy_stale_<date>.json` (`--apply` writes the ids and outcomes to `..._applied.json`, no text).
Reads GOOGLE_CLOUD_PROJECT and FIRESTORE_DATABASE from the environment or the repo's .env.

Usage:
    python -m scripts.migration.fact_s3_stamp_legacy_stale [--apply]
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

from google.api_core.exceptions import FailedPrecondition, NotFound  # noqa: E402
from google.cloud.firestore_v1.base_query import FieldFilter  # noqa: E402
from src.domain.entities import FactState  # noqa: E402
from src.domain.fact_lifecycle import lifecycle_event, with_event  # noqa: E402

MEMORY_DIR = project_root / "scripts" / "memory"
EVENT_ACTOR = "migration"
EVENT_REASON = "legacy stale, stamped at S3"
SCAN_FIELDS = ["account_id", "stale_since", "last_updated", "text", "lifecycle_events"]

STAMPED = "stamped"
CHANGED = "skipped_changed"
FAILED = "failed"


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else None


async def scan(db: Any, collection: str) -> tuple[list[tuple[Any, dict[str, Any], Any]], int]:
    """(snapshot reference, data, update_time) of each stale fact without `stale_since`, and the count that has one."""
    query = db.collection(collection).where(filter=FieldFilter("state", "==", FactState.STALE.value)).select(SCAN_FIELDS)
    candidates: list[tuple[Any, dict[str, Any], Any]] = []
    already = 0
    async for snap in query.stream():
        data = snap.to_dict() or {}
        if data.get("stale_since") is not None:
            already += 1
        else:
            candidates.append((snap.reference, data, snap.update_time))
    return candidates, already


async def stamp(db: Any, ref: Any, data: dict[str, Any], update_time: Any, now: datetime) -> str:
    """Write `stale_since` and one event, only if the document is unchanged since the scan."""
    event = lifecycle_event(now, FactState.STALE, FactState.STALE, EVENT_ACTOR, EVENT_REASON)
    events = with_event(list(data.get("lifecycle_events") or []), event)
    try:
        await ref.update({"stale_since": now, "lifecycle_events": events}, option=db.write_option(last_update_time=update_time))
    except (FailedPrecondition, NotFound):
        return CHANGED
    except Exception as exc:  # the type only: a message may carry document content
        print(f"  write failed: {type(exc).__name__}")
        return FAILED
    return STAMPED


async def run(db: Any, collection: str, apply: bool, out_dir: Path, now: Optional[datetime] = None) -> dict[str, int]:
    """Scan, report, and with `apply` stamp. Returns the counts it printed."""
    now = now or datetime.now(timezone.utc)
    candidates, already = await scan(db, collection)
    counts = {"candidates": len(candidates), "already_stamped": already, STAMPED: 0, CHANGED: 0, FAILED: 0}
    out_dir.mkdir(parents=True, exist_ok=True)
    day = f"{now:%Y-%m-%d}"
    if apply:
        outcomes: dict[str, str] = {}
        for ref, data, update_time in candidates:
            outcomes[ref.id] = await stamp(db, ref, data, update_time, now)
        for outcome in outcomes.values():
            counts[outcome] += 1
        report: dict[str, Any] = {"generated_at": now.isoformat(), "collection": collection, "outcomes": outcomes}
        path = out_dir / f"fact_s3_legacy_stale_{day}_applied.json"
    else:
        facts = [
            {
                "id": ref.id,
                "account_prefix": str(data.get("account_id") or "")[:8],
                "last_updated": _iso(data.get("last_updated")),
                "text": data.get("text"),
            }
            for ref, data, _ in candidates
        ]
        report = {"generated_at": now.isoformat(), "collection": collection, "facts": facts}
        path = out_dir / f"fact_s3_legacy_stale_{day}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Mode: {'APPLY' if apply else 'DRY-RUN'}")
    for name, value in counts.items():
        print(f"{name}: {value}")
    print(f"report: {path}")
    return counts


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the stamps (default: dry run)")
    args = parser.parse_args(argv)
    from scripts._firestore import async_client   # loads .env at import: only when run for real
    from src.config.environment import EnvironmentConfig
    collection = EnvironmentConfig().domain_facts_collection

    async def go() -> None:   # the client is built inside the loop it runs on
        await run(async_client(), collection, args.apply, MEMORY_DIR)

    asyncio.run(go())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

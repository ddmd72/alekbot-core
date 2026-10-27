#!/usr/bin/env python3
"""
Facts P2 (revised): re-open archived facts as stale, so the memory review looks at them again.

Selection (exactly one):
    --ids-file PATH     one fact id per line (blank lines and `#` comments ignored); the file lives under scripts/memory/
    --all-archived      every `archived` fact of every non-SYSTEM account, or of `--account ID` only
A selected fact that does not exist, is not `archived`, or belongs to the SYSTEM account is counted and left alone.

Optional `--expiry-plan PLAN` (a classify plan JSON under scripts/memory/, the documented S5 format: `decisions[]` with
`fact_id` and `fields` {expiry_kind, ttl_days, expires_at}; a minimal local reader, `scripts/consolidation/plan_inputs.py`):
the plan's three expiry fields are written in the same preconditioned write; a fact absent from the plan is reopened
without expiry and listed (`without_expiry`).

Per fact, `--apply` writes exactly: `lifecycle_fields(STALE, now)` (state stale, is_current, valid_to cleared),
`last_updated = now`, `stale_since` (the stored one is restored; a fact that never had one gets the moment of its
`valid_to`, then of its last trail entry, then `now`), and one `lifecycle_events` entry {archived -> stale, actor
migration, "reopened under P2 revised"}. Text and vectors are never touched, nor is expiry without `--expiry-plan`.

Dry run by default (reads only). `--apply` writes each document under the `update_time` of the scanned snapshot; a
document changed (or deleted) since the scan is skipped and counted (re-run to pick it up). A second `--apply` reopens
nothing: the facts are stale by then.

stdout prints counts only. The dry run writes the gitignored `scripts/memory/fact_p2_reopen_<date>.json` (id, account
prefix, text, domain, type, reported_date, valid_to, stale_since, last trail entry); `--apply` writes the ids and outcomes
to `..._applied.json` (no text). Reads GOOGLE_CLOUD_PROJECT and FIRESTORE_DATABASE from the environment or the repo's .env.

Usage:
    python -m scripts.migration.fact_p2_reopen_archives --all-archived [--account ID] [--apply]
    python -m scripts.migration.fact_p2_reopen_archives --ids-file scripts/memory/ids.txt [--apply]
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
from src.domain.fact_lifecycle import lifecycle_event, lifecycle_fields, with_event  # noqa: E402
from src.domain.prompt import SYSTEM_ACCOUNT_ID  # noqa: E402
from scripts.consolidation import plan_inputs  # noqa: E402
from scripts.consolidation.plan_inputs import SelectionError  # noqa: E402

MEMORY_DIR = project_root / "scripts" / "memory"
EVENT_ACTOR = "migration"
EVENT_REASON = "reopened under P2 revised"
SCAN_FIELDS = [
    "account_id", "state", "text", "domain", "type", "reported_date", "valid_to", "stale_since", "lifecycle_events",
]

REOPENED = "reopened"
CHANGED = "skipped_changed"
FAILED = "failed"


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else None


def _plain(value: Any) -> Any:
    return getattr(value, "value", value)


def read_ids(path: Path, memory_dir: Path = MEMORY_DIR) -> list[str]:
    """The ids of `path`, in order and without duplicates; the file must sit under `memory_dir` (ids are private)."""
    return plan_inputs.read_ids(path, memory_dir)


Candidate = tuple[Any, dict[str, Any], Any]


async def scan(
    db: Any, collection: str, *, ids: Optional[list[str]] = None, account: Optional[str] = None
) -> tuple[list[Candidate], dict[str, list[str]]]:
    """(reference, data, update_time) of each selected `archived` fact, and the ids left out by reason
    (`not_found`, `not_archived`, `system_account`). `ids` selects those documents; without it every archived fact."""
    left_out: dict[str, list[str]] = {"not_found": [], "not_archived": [], "system_account": []}
    candidates: list[Candidate] = []

    def take(ref: Any, data: dict[str, Any], update_time: Any) -> None:
        if data.get("state") != FactState.ARCHIVED.value:
            left_out["not_archived"].append(ref.id)
        elif data.get("account_id") == SYSTEM_ACCOUNT_ID:
            left_out["system_account"].append(ref.id)
        else:
            candidates.append((ref, data, update_time))

    if ids is not None:
        for fact_id in ids:
            snap = await db.collection(collection).document(fact_id).get(field_paths=SCAN_FIELDS)
            if not snap.exists:
                left_out["not_found"].append(fact_id)
            else:
                take(snap.reference, snap.to_dict() or {}, snap.update_time)
        return candidates, left_out

    query = db.collection(collection).where(filter=FieldFilter("state", "==", FactState.ARCHIVED.value))
    if account is not None:
        query = query.where(filter=FieldFilter("account_id", "==", account))
    async for snap in query.select(SCAN_FIELDS).stream():
        take(snap.reference, snap.to_dict() or {}, snap.update_time)
    return candidates, left_out


def last_entry(data: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The newest trail entry of the fact, as plain JSON values."""
    events = data.get("lifecycle_events") or []
    if not events:
        return None
    entry = dict(events[-1])
    entry["at"] = _iso(entry.get("at")) or entry.get("at")
    return entry


def restored_stale_since(data: dict[str, Any], now: datetime) -> datetime:
    """The `stale_since` the archive kept; for a fact that never had one, the moment its validity closed (the archive)."""
    events = data.get("lifecycle_events") or []
    for value in (data.get("stale_since"), data.get("valid_to"), events[-1].get("at") if events else None):
        if isinstance(value, datetime):
            return value
    return now


def expiry_write(fields: dict[str, Any]) -> dict[str, Any]:
    """A plan entry's expiry fields as stored (the kind as its string value)."""
    return {"expiry_kind": fields["expiry_kind"].value, "ttl_days": fields["ttl_days"], "expires_at": fields["expires_at"]}


def reopen_fields(data: dict[str, Any], now: datetime, expiry: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The whole write of one reopened fact; `expiry` (a plan entry's fields) adds the three expiry fields."""
    event = lifecycle_event(now, FactState.ARCHIVED, FactState.STALE, EVENT_ACTOR, EVENT_REASON)
    return {
        **lifecycle_fields(FactState.STALE, now),
        "last_updated": now,
        "stale_since": restored_stale_since(data, now),
        "lifecycle_events": with_event(list(data.get("lifecycle_events") or []), event),
        **(expiry_write(expiry) if expiry is not None else {}),
    }


async def reopen(
    db: Any, ref: Any, data: dict[str, Any], update_time: Any, now: datetime, expiry: Optional[dict[str, Any]] = None
) -> str:
    """Write the reopen, only if the document is unchanged since the scan."""
    try:
        await ref.update(reopen_fields(data, now, expiry), option=db.write_option(last_update_time=update_time))
    except (FailedPrecondition, NotFound):
        return CHANGED
    except Exception as exc:  # the type only: a message may carry document content
        print(f"  write failed: {type(exc).__name__}")
        return FAILED
    return REOPENED


async def run(
    db: Any,
    collection: str,
    apply: bool,
    out_dir: Path,
    now: Optional[datetime] = None,
    *,
    ids: Optional[list[str]] = None,
    account: Optional[str] = None,
    expiry_plan: Optional[dict[str, dict[str, Any]]] = None,
) -> dict[str, int]:
    """Scan, report, and with `apply` reopen. Returns the counts it printed. With `expiry_plan` (fact id -> fields) a
    reopened fact takes its plan entry's expiry; one absent from the plan is reopened without and listed."""
    now = now or datetime.now(timezone.utc)
    candidates, left_out = await scan(db, collection, ids=ids, account=account)
    counts = {
        "candidates": len(candidates),
        **{name: len(found) for name, found in left_out.items()},
        REOPENED: 0, CHANGED: 0, FAILED: 0,
    }
    without_expiry = [] if expiry_plan is None else [ref.id for ref, _, _ in candidates if ref.id not in expiry_plan]
    if expiry_plan is not None:
        counts["without_expiry"] = len(without_expiry)
    out_dir.mkdir(parents=True, exist_ok=True)
    day = f"{now:%Y-%m-%d}"
    if apply:
        outcomes: dict[str, str] = {}
        for ref, data, update_time in candidates:
            outcomes[ref.id] = await reopen(db, ref, data, update_time, now, (expiry_plan or {}).get(ref.id))
        for outcome in outcomes.values():
            counts[outcome] += 1
        report: dict[str, Any] = {"generated_at": now.isoformat(), "collection": collection, "outcomes": outcomes}
        if expiry_plan is not None:
            report["without_expiry"] = without_expiry
        path = out_dir / f"fact_p2_reopen_{day}_applied.json"
    else:
        facts = [
            {
                "id": ref.id,
                "account_prefix": str(data.get("account_id") or "")[:8],
                "text": data.get("text"),
                "domain": _plain(data.get("domain")),
                "type": _plain(data.get("type")),
                "reported_date": _iso(data.get("reported_date")),
                "valid_to": _iso(data.get("valid_to")),
                "stale_since": _iso(data.get("stale_since")),
                "last_event": last_entry(data),
            }
            for ref, data, _ in candidates
        ]
        report = {"generated_at": now.isoformat(), "collection": collection, "facts": facts, "left_out": left_out}
        if expiry_plan is not None:
            report["without_expiry"] = without_expiry
        path = out_dir / f"fact_p2_reopen_{day}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Mode: {'APPLY' if apply else 'DRY-RUN'}")
    for name, value in counts.items():
        print(f"{name}: {value}")
    print(f"report: {path}")
    return counts


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--ids-file", type=Path, help="fact ids, one per line; a file under scripts/memory/")
    selection.add_argument("--all-archived", action="store_true", help="every archived fact of the non-SYSTEM accounts")
    parser.add_argument("--account", help="with --all-archived: only this account")
    parser.add_argument("--expiry-plan", type=Path, help="a classify plan JSON under scripts/memory/: its expiry fields are "
                        "written with the reopen; a fact absent from the plan is reopened without expiry and listed")
    parser.add_argument("--apply", action="store_true", help="write the reopens (default: dry run)")
    args = parser.parse_args(argv)
    if args.account and not args.all_archived:
        parser.error("--account goes with --all-archived")
    try:
        ids = read_ids(args.ids_file, MEMORY_DIR) if args.ids_file else None
        plan = plan_inputs.read_expiry_plan(args.expiry_plan, MEMORY_DIR) if args.expiry_plan else None
    except (SelectionError, OSError) as exc:
        parser.error(str(exc))
    from scripts._firestore import async_client   # loads .env at import: only when run for real
    from src.config.environment import EnvironmentConfig
    collection = EnvironmentConfig().domain_facts_collection

    async def go() -> None:   # the client is built inside the loop it runs on
        await run(async_client(), collection, args.apply, MEMORY_DIR, ids=ids, account=args.account, expiry_plan=plan)

    asyncio.run(go())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

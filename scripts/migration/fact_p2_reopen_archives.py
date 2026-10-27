#!/usr/bin/env python3
"""
Facts P2 (revised): re-open the facts on the owner's reviewed list as stale, so the memory review looks at them again.

Selection: `--ids-file PATH` only, one fact id per line (blank lines and `#` comments ignored), a file under
scripts/memory/ (the owner-reviewed list). A listed fact in state `archived` or `invalidated` is reopened. A fact that
does not exist, is in any other state (`not_closed`), or belongs to the SYSTEM account is counted and left alone.

Optional `--expiry-plan PLAN` (a classify plan JSON under scripts/memory/, the documented S5 format: `decisions[]` with
`fact_id` and `fields` {expiry_kind, ttl_days, expires_at}; a minimal local reader, `scripts/consolidation/plan_inputs.py`):
the plan's three expiry fields are written in the same preconditioned write; a fact absent from the plan is reopened
without expiry and listed (`without_expiry`).

Per fact, `--apply` writes exactly: `lifecycle_fields(STALE, now)` (state stale, is_current, valid_to cleared),
`last_updated = now`, `stale_since` (the stored one is restored; a fact that never had one gets the moment of its
`valid_to`, then of its last trail entry, then `now`), and one `lifecycle_events` entry {<its real state> -> stale, actor
migration, "reopened under P2 revised"}. Text and vectors are never touched, nor is expiry without `--expiry-plan`.

Dry run by default (reads only). `--apply` writes each document under the `update_time` of the scanned snapshot; a
document changed (or deleted) since the scan is skipped and counted (re-run to pick it up). A second `--apply` reopens
nothing: the facts are stale by then.

stdout prints counts only. The dry run writes the gitignored `scripts/memory/fact_p2_reopen_<date>.json` (id, account
prefix, text, domain, type, state, reported_date, valid_to, stale_since, last trail entry); `--apply` writes the ids and
outcomes to `..._applied.json` (no text). Reads GOOGLE_CLOUD_PROJECT and FIRESTORE_DATABASE from the environment or the
repo's .env.

Usage:
    python -m scripts.migration.fact_p2_reopen_archives --ids-file scripts/memory/ids.txt [--expiry-plan PLAN] [--apply]
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
from src.domain.entities import FactState  # noqa: E402
from src.domain.fact_lifecycle import lifecycle_event, lifecycle_fields, with_event  # noqa: E402
from src.domain.prompt import SYSTEM_ACCOUNT_ID  # noqa: E402
from scripts.consolidation import plan_inputs  # noqa: E402
from scripts.consolidation.plan_inputs import SelectionError  # noqa: E402

MEMORY_DIR = project_root / "scripts" / "memory"
EVENT_ACTOR = "migration"
EVENT_REASON = "reopened under P2 revised"
REOPENABLE = frozenset({FactState.ARCHIVED.value, FactState.INVALIDATED.value})
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


async def scan(db: Any, collection: str, *, ids: list[str]) -> tuple[list[Candidate], dict[str, list[str]]]:
    """(reference, data, update_time) of each listed fact that is `archived` or `invalidated`, and the listed ids left
    out by reason (`not_found`, `not_closed`, `system_account`)."""
    left_out: dict[str, list[str]] = {"not_found": [], "not_closed": [], "system_account": []}
    candidates: list[Candidate] = []
    for fact_id in ids:
        snap = await db.collection(collection).document(fact_id).get(field_paths=SCAN_FIELDS)
        if not snap.exists:
            left_out["not_found"].append(fact_id)
            continue
        data = snap.to_dict() or {}
        if _plain(data.get("state")) not in REOPENABLE:
            left_out["not_closed"].append(fact_id)
        elif data.get("account_id") == SYSTEM_ACCOUNT_ID:
            left_out["system_account"].append(fact_id)
        else:
            candidates.append((snap.reference, data, snap.update_time))
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
    """The `stale_since` the closing kept; for a fact that never had one, the moment its validity closed."""
    events = data.get("lifecycle_events") or []
    for value in (data.get("stale_since"), data.get("valid_to"), events[-1].get("at") if events else None):
        if isinstance(value, datetime):
            return value
    return now


def expiry_write(fields: dict[str, Any]) -> dict[str, Any]:
    """A plan entry's expiry fields as stored (the kind as its string value)."""
    return {"expiry_kind": fields["expiry_kind"].value, "ttl_days": fields["ttl_days"], "expires_at": fields["expires_at"]}


def reopen_fields(data: dict[str, Any], now: datetime, expiry: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The whole write of one reopened fact; `expiry` (a plan entry's fields) adds the three expiry fields. The trail
    entry's `from` is the fact's real state."""
    event = lifecycle_event(now, FactState(_plain(data["state"])), FactState.STALE, EVENT_ACTOR, EVENT_REASON)
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
    ids: list[str],
    expiry_plan: Optional[dict[str, dict[str, Any]]] = None,
) -> dict[str, int]:
    """Scan the listed `ids`, report, and with `apply` reopen. Returns the counts it printed. With `expiry_plan` (fact id
    -> fields) a reopened fact takes its plan entry's expiry; one absent from the plan is reopened without and listed."""
    now = now or datetime.now(timezone.utc)
    candidates, left_out = await scan(db, collection, ids=ids)
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
                "state": _plain(data.get("state")),
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
    parser.add_argument("--ids-file", type=Path, required=True, help="fact ids, one per line; a file under scripts/memory/")
    parser.add_argument("--expiry-plan", type=Path, help="a classify plan JSON under scripts/memory/: its expiry fields are "
                        "written with the reopen; a fact absent from the plan is reopened without expiry and listed")
    parser.add_argument("--apply", action="store_true", help="write the reopens (default: dry run)")
    args = parser.parse_args(argv)
    try:
        ids = read_ids(args.ids_file, MEMORY_DIR)
        plan = plan_inputs.read_expiry_plan(args.expiry_plan, MEMORY_DIR) if args.expiry_plan else None
    except (SelectionError, OSError) as exc:
        parser.error(str(exc))
    from scripts._firestore import async_client   # loads .env at import: only when run for real
    from src.config.environment import EnvironmentConfig
    collection = EnvironmentConfig().domain_facts_collection

    async def go() -> None:   # the client is built inside the loop it runs on
        await run(async_client(), collection, args.apply, MEMORY_DIR, ids=ids, expiry_plan=plan)

    asyncio.run(go())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

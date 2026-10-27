#!/usr/bin/env python3
"""
Facts P2 rollback: bring the memory-review question documents back to the S4 shape the old code reads.

The P2 model stores five fields the S4 model refuses (`extra="forbid"`): `question`, `texts`, `origin`, `delivery`,
`fallback_attempts`. `model_dump` writes them even at their defaults, so EVERY document the P2 code wrote carries them.
This script therefore removes those five keys from every document that has one. Beyond that, a P2 record (any of: a
`question` other than `still_true`, non-empty `texts`, an `origin` other than `review`, a `delivery` other than `chat`,
`fallback_attempts > 0`, `fallback == "history"`, an `answer` outside still_true/ended/never/dont_know) is also made
legal for S4:
    digest delivery  -> the document is deleted
    closed           -> closed_by "resolved_elsewhere", answer and answer_source cleared (`purge_after` kept), and the
                        fallback fields as for an open one (a closed `history` fallback is no S4 value either)
    open, sent       -> (status pending, asked or unanswered: its message went out with P2 options) CLOSED as
                        `resolved_elsewhere` with the five-key strip, never converted: the old code would read the owner's
                        digit with S4 meanings ("3" -> never) and invalidate a real event. `closed_at` and
                        `purge_after` (90 days) are stamped at apply.
    open, queued     -> converted: fallback "revive", noise False, fallback_date cleared
A document with only the default-valued P2 keys is just stripped.

Run it once more after the traffic switch (the P2 code may have written records between the first run and the switch).

Dry run by default (reads only). `--apply` writes each document under the `update_time` of the scanned snapshot; a
document changed (or deleted) since the scan is skipped and counted (re-run to pick it up). A second `--apply` does nothing.

stdout prints counts only. The report (ids and the action, no quoted text) goes to the gitignored
`scripts/memory/fact_p2_rollback_<date>.json` (`--apply`: `..._applied.json`, ids and outcomes).
Reads GOOGLE_CLOUD_PROJECT and FIRESTORE_DATABASE from the environment or the repo's .env.

Usage:
    python -m scripts.migration.fact_p2_rollback_questions [--apply]
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
from google.cloud.firestore_v1 import DELETE_FIELD  # noqa: E402
from src.domain.fact_review_question import QUESTION_RETENTION  # noqa: E402

MEMORY_DIR = project_root / "scripts" / "memory"
P2_KEYS = ("question", "texts", "origin", "delivery", "fallback_attempts")
S4_ANSWERS = frozenset({"still_true", "ended", "never", "dont_know"})
SCAN_FIELDS = [*P2_KEYS, "fallback", "answer", "status", "delivery"]

# Open statuses whose question message was (or may have been) sent.
SENT_STATUSES = frozenset({"pending", "asked", "unanswered"})

DELETE = "deleted"
CLOSE = "closed_sent"
REWRITE = "rewritten"
STRIP = "stripped"
CHANGED = "skipped_changed"
FAILED = "failed"


def is_p2_record(data: dict[str, Any]) -> bool:
    """A document a P2 feature made, not just one that carries the five default-valued keys."""
    return bool(
        data.get("question", "still_true") != "still_true"
        or data.get("texts")
        or data.get("origin", "review") != "review"
        or data.get("delivery", "chat") != "chat"
        or (data.get("fallback_attempts") or 0) > 0
        or data.get("fallback") == "history"
        or data.get("answer") not in (None, *S4_ANSWERS)
    )


def plan_of(data: dict[str, Any]) -> Optional[tuple[str, dict[str, Any]]]:
    """(action, fields to write) for one document, or None when it has nothing P2 on it. A delete writes no fields."""
    p2 = is_p2_record(data)
    removals = {key: DELETE_FIELD for key in P2_KEYS if key in data}
    if not p2 and not removals:
        return None
    if not p2:
        return STRIP, removals
    if data.get("delivery") == "digest":
        return DELETE, {}
    fallback = {"fallback": "revive", "noise": False, "fallback_date": None}   # the S4 model has no `history` fallback
    if data.get("status") == "closed":
        return REWRITE, {**removals, **fallback, "closed_by": "resolved_elsewhere", "answer": None, "answer_source": None}
    if data.get("status") in SENT_STATUSES:
        # Its message went out with P2 options: the old code would read the owner's digit with S4 meanings ("3" -> never)
        # and invalidate a real event. Closed, never converted; `closed_at` and `purge_after` are stamped at apply.
        return CLOSE, {
            **removals, **fallback, "status": "closed", "closed_by": "resolved_elsewhere", "answer": None,
            "answer_source": None,
        }
    return REWRITE, {**removals, **fallback}


async def scan(db: Any, collection: str) -> list[tuple[Any, dict[str, Any], Any, bool, str, dict[str, Any]]]:
    """(reference, data, update_time, is_p2_record, action, fields) of every document that has something to roll back."""
    found = []
    async for snap in db.collection(collection).select(SCAN_FIELDS).stream():
        data = snap.to_dict() or {}
        plan = plan_of(data)
        if plan is not None:
            found.append((snap.reference, data, snap.update_time, is_p2_record(data), plan[0], plan[1]))
    return found


async def apply_one(db: Any, ref: Any, update_time: Any, action: str, fields: dict[str, Any]) -> str:
    """Write one plan, only if the document is unchanged since the scan."""
    option = db.write_option(last_update_time=update_time)
    try:
        if action == DELETE:
            await ref.delete(option=option)
        else:
            await ref.update(fields, option=option)
    except (FailedPrecondition, NotFound):
        return CHANGED
    except Exception as exc:  # the type only: a message may carry document content
        print(f"  write failed: {type(exc).__name__}")
        return FAILED
    return action


async def run(db: Any, collection: str, apply: bool, out_dir: Path, now: Optional[datetime] = None) -> dict[str, int]:
    """Scan, report, and with `apply` roll back. Returns the counts it printed."""
    now = now or datetime.now(timezone.utc)
    found = await scan(db, collection)
    counts = {
        "p2_records": sum(1 for item in found if item[3]),
        "p2_defaults_only": sum(1 for item in found if not item[3]),
        DELETE: 0, CLOSE: 0, REWRITE: 0, STRIP: 0, CHANGED: 0, FAILED: 0,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    day = f"{now:%Y-%m-%d}"
    if apply:
        outcomes: dict[str, str] = {}
        for ref, _, update_time, _, action, fields in found:
            stamps = {"closed_at": now, "purge_after": now + QUESTION_RETENTION} if action == CLOSE else {}
            outcomes[ref.id] = await apply_one(db, ref, update_time, action, {**fields, **stamps})
        for outcome in outcomes.values():
            counts[outcome] += 1
        report: dict[str, Any] = {"generated_at": now.isoformat(), "collection": collection, "outcomes": outcomes}
        path = out_dir / f"fact_p2_rollback_{day}_applied.json"
    else:
        plans = {ref.id: {"p2_record": p2, "action": action} for ref, _, _, p2, action, _ in found}
        report = {"generated_at": now.isoformat(), "collection": collection, "plans": plans}
        path = out_dir / f"fact_p2_rollback_{day}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Mode: {'APPLY' if apply else 'DRY-RUN'}")
    for name, value in counts.items():
        print(f"{name}: {value}")
    print(f"report: {path}")
    return counts


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the rollback (default: dry run)")
    args = parser.parse_args(argv)
    from scripts._firestore import async_client   # loads .env at import: only when run for real
    from src.config.environment import EnvironmentConfig
    collection = EnvironmentConfig().fact_review_questions_collection

    async def go() -> None:   # the client is built inside the loop it runs on
        await run(async_client(), collection, args.apply, MEMORY_DIR)

    asyncio.run(go())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

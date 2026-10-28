"""
Re-read drive files whose content read failed (USER_DRIVE_INDEXING_RFC §8.2, §3.4). A record that failed MAX_ATTEMPTS
times is `failed` with a per-file reason, and that outcome stands until the file's content version or
EXTRACTION_VERSION changes, so a fix of the failure's cause never reaches the files that already failed, and bumping
EXTRACTION_VERSION would re-read the whole drive. This script makes only the failed records the owner selects (one
reason, optionally one kind) due for a fresh read: `content_requeued` (the planner) sets each one pending with its
attempts reset, due at once in class 3, its card, chunks and readable generation kept. The next tick reads them through
its normal path. Records that are not `failed` with the selected reason are never touched.

Dry run by default: prints counts by kind and reason (never names or paths) and how many would still be held (a record
the bot cannot open, or one waiting for card headroom), and writes nothing. `--apply` takes the user's lease (refused
while a tick runs: retry in a few minutes), selects the records again under it, writes them and releases it.

Usage (project and credentials from the environment / ADC, collections from APP_ENV, default development):
    python scripts/infrastructure/drive_content_requeue.py --user <user_id> [--reason extract_error] [--kind <kind>] [--apply]
Exit code: 0 (also for a dry run or nothing to requeue), 1 when nothing is stored for the user or the lease is busy.
"""
import argparse
import asyncio
import os
import sys
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.domain.drive_index import DriveIndexRecord, Reason, RecordState  # noqa: E402
from src.domain.drive_index_planner import (  # noqa: E402
    FAILED_REASONS, content_due, content_requeued, triage_due,
)
from src.ports.drive_index_repository import DriveIndexRepository  # noqa: E402

_HOLD = timedelta(minutes=10)    # the lease is held for the writes of one run
_PAGE = 500
_WRITE_BATCH = 100
REASONS = tuple(sorted(r.value for r in FAILED_REASONS))


@dataclass
class Plan:
    """The selected records, each as the requeue would write it; counts by (kind, reason) and of those still held."""
    records: List[DriveIndexRecord] = field(default_factory=list)
    by_kind: Counter = field(default_factory=Counter)
    held: int = 0


def is_selected(rec: DriveIndexRecord, reason: Reason, kind: Optional[str]) -> bool:
    return (not rec.is_folder and rec.state is RecordState.FAILED and rec.reason is reason
            and (kind is None or rec.kind == kind))


def reads_next_tick(rec: DriveIndexRecord, now: datetime) -> bool:
    """The requeued record's content or triage work is due at `now` (the tick's own tests, §8.1)."""
    return rec.due_at is not None and rec.due_at <= now and (content_due(rec, now) or triage_due(rec, now))


def build_plan(records: List[DriveIndexRecord], reason: Reason, kind: Optional[str], now: datetime) -> Plan:
    plan = Plan()
    for rec in sorted(records, key=lambda r: r.item_id):
        if not is_selected(rec, reason, kind):
            continue
        requeued = content_requeued(rec, now)
        plan.records.append(requeued)
        plan.by_kind[(rec.kind, reason.value)] += 1
        if not reads_next_tick(requeued, now):
            plan.held += 1
    return plan


def summary(plan: Plan) -> str:
    counts = ", ".join(f"{kind or '?'}/{reason}: {n}" for (kind, reason), n in sorted(plan.by_kind.items()))
    held = f"; {plan.held} of them stay held (cannot be opened, or wait for card headroom)" if plan.held else ""
    return f"{len(plan.records)} failed files ({counts}){held}"


async def load_plan(repo: DriveIndexRepository, user_id: str, reason: Reason, kind: Optional[str],
                    now: datetime) -> Plan:
    records: List[DriveIndexRecord] = []
    after: Optional[str] = None
    while True:
        page = await repo.records_page(user_id, after, _PAGE)
        records.extend(r for r in page if is_selected(r, reason, kind))
        if len(page) < _PAGE:
            return build_plan(records, reason, kind, now)
        after = page[-1].item_id


async def requeue(repo: DriveIndexRepository, user_id: str, *, reason: Reason, kind: Optional[str], apply: bool,
                  now: datetime, owner: str) -> Tuple[int, str]:
    """Returns (exit code, message). With `apply` the selection is made again under the lease, so a record a tick
    changed since the dry run is judged as it is now."""
    if reason not in FAILED_REASONS:
        raise ValueError(f"{reason.value} is not a per-file failure")
    if await repo.get_sync_state(user_id) is None:
        return 1, "no sync state: nothing is indexed for this user"
    if not apply:
        plan = await load_plan(repo, user_id, reason, kind, now)
        if not plan.records:
            return 0, "nothing to requeue"
        return 0, f"dry run: {summary(plan)} would be read again on the next tick; re-run with --apply"
    if await repo.acquire_lease(user_id, owner, now, now + _HOLD, create=False) is None:
        return 1, "a tick holds the lease (or the state just went away); retry in a few minutes"
    try:
        plan = await load_plan(repo, user_id, reason, kind, now)
        for start in range(0, len(plan.records), _WRITE_BATCH):
            await repo.upsert_records(plan.records[start:start + _WRITE_BATCH])
    finally:
        await repo.release_lease(user_id, owner)
    if not plan.records:
        return 0, "nothing to requeue"
    return 0, f"requeued: {summary(plan)}; the next tick reads them again"


async def _main(user_id: str, reason: Reason, kind: Optional[str], apply: bool) -> int:
    from google.cloud import firestore

    from src.adapters.firestore_drive_index_repo import FirestoreDriveIndexRepository
    from src.config.environment import EnvironmentConfig

    env = EnvironmentConfig()
    db = firestore.AsyncClient(project=os.getenv("GOOGLE_CLOUD_PROJECT") or None, database=env.firestore_database_id)
    code, message = await requeue(FirestoreDriveIndexRepository(db, env), user_id, reason=reason, kind=kind,
                                  apply=apply, now=datetime.now(timezone.utc), owner=f"requeue-{uuid.uuid4().hex}")
    print(message)
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--user", required=True, help="the user whose failed files are read again")
    parser.add_argument("--reason", default=Reason.EXTRACT_ERROR.value, choices=REASONS,
                        help="the failure reason to select (default: extract_error)")
    parser.add_argument("--kind", default=None, help="only records of this kind (e.g. pdf, docx)")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run, counts only)")
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args.user, Reason(args.reason), args.kind, args.apply)))

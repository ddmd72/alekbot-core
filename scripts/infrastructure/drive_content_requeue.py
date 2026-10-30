"""
Re-read drive files whose content outcome stands although a fix would now read them better (USER_DRIVE_INDEXING_RFC
§8.2, §3.4, §5.4, §24.5). An outcome stands until the file's content version or EXTRACTION_VERSION changes, so a fix
never reaches the files read before it, and bumping EXTRACTION_VERSION would re-read the whole drive. This script makes
only the records the owner selects due for a fresh read, one selection per run, optionally one kind:
- failed (default, `--reason`): a record that failed MAX_ATTEMPTS times with that per-file reason; `content_requeued`
  (the planner) sets it pending with its attempts reset;
- `--truncated`: an indexed record whose text was cut (an older MAX_CHUNKS_PER_FILE); the next read chunks it under
  today's caps;
- `--no-text`: an image whose `text` verdict holds but which the reader got no text from (before the rendition
  step-down, a rendition over the vision cap was demoted unread); it goes to the reader again, never to triage.
The last two go through `content_reread` (the planner). Each selected record is due at once in class 3, its card,
chunks and readable generation kept (served until the new generation commits). The next tick reads them through its
normal path. Records outside the selection are never touched. A truncated file whose longer text no longer fits the
user's chunk cap is demoted user_chunk_cap by that read, like any other read (released when headroom comes back).

Dry run by default: prints counts by kind and selection (never names or paths), how many would still be held (a record
the bot cannot open, or one waiting for card headroom) and how many go to the reader (each re-read is billed), and writes
nothing. `--apply` takes the connection's lease (refused while a tick runs: retry in a few minutes), selects the
records again under it, writes them and releases it. The script acts on one connection (MULTI_DRIVE_RFC §8):
`--connection` names it, else the user's one live connection.

Usage (project and credentials from the environment / ADC, collections from APP_ENV, default development):
    python scripts/infrastructure/drive_content_requeue.py --user <user_id> [--connection <id>]
        [--reason extract_error | --truncated | --no-text] [--kind <kind>] [--apply]
Exit code: 0 (also for a dry run or nothing to requeue), 1 when nothing is stored for the connection or the lease is busy.
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

from scripts.infrastructure.drive_connection_arg import resolve_connection  # noqa: E402
from src.domain.connection_scope import ConnectionScope  # noqa: E402
from src.domain.drive_extraction import READER, chain_for  # noqa: E402
from src.domain.drive_index import DriveIndexRecord, Reason, RecordState  # noqa: E402
from src.domain.drive_index_planner import (  # noqa: E402
    FAILED_REASONS, content_due, content_reread, content_requeued, rereadable, triage_due,
)
from src.ports.drive_index_repository import DriveIndexRepository  # noqa: E402

_HOLD = timedelta(minutes=10)    # the lease is held for the writes of one run
_PAGE = 500
_WRITE_BATCH = 100
REASONS = tuple(sorted(r.value for r in FAILED_REASONS))
FAILED, TRUNCATED, NO_TEXT = "failed", "truncated", "no_text"   # the selections
_NOUN = {FAILED: "failed files", TRUNCATED: "truncated files", NO_TEXT: "images with no text"}


@dataclass
class Plan:
    """The selected records, each as the requeue would write it; counts by (kind, label), of those still held and of
    those the reader reads."""
    records: List[DriveIndexRecord] = field(default_factory=list)
    by_kind: Counter = field(default_factory=Counter)
    held: int = 0
    reader: int = 0
    select: str = FAILED


def is_selected(rec: DriveIndexRecord, reason: Optional[Reason], kind: Optional[str], select: str = FAILED) -> bool:
    if rec.is_folder or (kind is not None and rec.kind != kind):
        return False
    if select == FAILED:
        return rec.state is RecordState.FAILED and rec.reason is reason
    wanted = RecordState.INDEXED if select == TRUNCATED else RecordState.METADATA_ONLY
    return rec.state is wanted and rereadable(rec)


def reads_next_tick(rec: DriveIndexRecord, now: datetime) -> bool:
    """The requeued record's content or triage work is due at `now` (the tick's own tests, §8.1)."""
    return rec.due_at is not None and rec.due_at <= now and (content_due(rec, now) or triage_due(rec, now))


def goes_to_reader(rec: DriveIndexRecord) -> bool:
    """The reader reads it: its kind starts there (an image), or the reader decided its last read (a scan)."""
    return chain_for(rec.kind)[0] == READER or (rec.extracted_by or "").rpartition("@")[0] == READER


def build_plan(records: List[DriveIndexRecord], reason: Optional[Reason], kind: Optional[str], now: datetime,
               select: str = FAILED) -> Plan:
    plan = Plan(select=select)
    label = reason.value if select == FAILED and reason is not None else select
    for rec in sorted(records, key=lambda r: r.item_id):
        if not is_selected(rec, reason, kind, select):
            continue
        requeued = content_requeued(rec, now) if select == FAILED else content_reread(rec, now)
        plan.records.append(requeued)
        plan.by_kind[(rec.kind, label)] += 1
        if not reads_next_tick(requeued, now):
            plan.held += 1
        if goes_to_reader(rec):
            plan.reader += 1
    return plan


def summary(plan: Plan) -> str:
    counts = ", ".join(f"{kind or '?'}/{label}: {n}" for (kind, label), n in sorted(plan.by_kind.items()))
    held = f"; {plan.held} of them stay held (cannot be opened, or wait for card headroom)" if plan.held else ""
    reader = f"; {plan.reader} of them go to the reader (billed)" if plan.reader else ""
    return f"{len(plan.records)} {_NOUN[plan.select]} ({counts}){held}{reader}"


async def load_plan(repo: DriveIndexRepository, scope: ConnectionScope, reason: Optional[Reason], kind: Optional[str],
                    now: datetime, select: str = FAILED) -> Plan:
    records: List[DriveIndexRecord] = []
    after: Optional[str] = None
    while True:
        page = await repo.records_page(scope, after, _PAGE)
        records.extend(r for r in page if is_selected(r, reason, kind, select))
        if len(page) < _PAGE:
            return build_plan(records, reason, kind, now, select)
        after = page[-1].item_id


async def requeue(repo: DriveIndexRepository, scope: ConnectionScope, *, reason: Optional[Reason], kind: Optional[str],
                  apply: bool, now: datetime, owner: str, select: str = FAILED) -> Tuple[int, str]:
    """Returns (exit code, message). `select` is FAILED (with a per-file `reason`), TRUNCATED or NO_TEXT (no
    `reason`). With `apply` the selection is made again under the lease, so a record a tick changed since the dry run
    is judged as it is now."""
    if select == FAILED and reason not in FAILED_REASONS:
        raise ValueError(f"{reason.value if reason else None} is not a per-file failure")
    if select not in _NOUN or (select != FAILED and reason is not None):
        raise ValueError(f"selection {select} takes no failure reason")
    if await repo.get_sync_state(scope) is None:
        return 1, "no sync state: nothing is indexed for this connection"
    if not apply:
        plan = await load_plan(repo, scope, reason, kind, now, select)
        if not plan.records:
            return 0, "nothing to requeue"
        return 0, f"dry run: {summary(plan)} would be read again on the next tick; re-run with --apply"
    if await repo.acquire_lease(scope, owner, now, now + _HOLD, create=False) is None:
        return 1, "a tick holds the lease (or the state just went away); retry in a few minutes"
    try:
        plan = await load_plan(repo, scope, reason, kind, now, select)
        for start in range(0, len(plan.records), _WRITE_BATCH):
            await repo.upsert_records(plan.records[start:start + _WRITE_BATCH])
    finally:
        await repo.release_lease(scope, owner)
    if not plan.records:
        return 0, "nothing to requeue"
    return 0, f"requeued: {summary(plan)}; the next tick reads them again"


def selection_of(args: argparse.Namespace) -> Tuple[str, Optional[Reason]]:
    """The CLI's one selection: `--truncated`, `--no-text`, or failed with `--reason` (default extract_error)."""
    if args.truncated:
        return TRUNCATED, None
    if args.no_text:
        return NO_TEXT, None
    return FAILED, Reason(args.reason or Reason.EXTRACT_ERROR.value)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=(__doc__ or "").strip().split("\n\n")[0])
    p.add_argument("--user", required=True, help="the user whose files are read again")
    p.add_argument("--connection", default=None, help="the connection (default: the user's one live connection)")
    which = p.add_mutually_exclusive_group()
    which.add_argument("--reason", default=None, choices=REASONS,
                       help="failed records with this failure reason (the default selection: extract_error)")
    which.add_argument("--truncated", action="store_true", help="indexed records whose text was cut")
    which.add_argument("--no-text", action="store_true",
                       help="images with a text verdict the reader got no text from")
    p.add_argument("--kind", default=None, help="only records of this kind (e.g. pdf, docx, image)")
    p.add_argument("--apply", action="store_true", help="write (default: dry run, counts only)")
    return p


async def _main(user_id: str, connection_id: Optional[str], select: str, reason: Optional[Reason],
                kind: Optional[str], apply: bool) -> int:
    from google.cloud import firestore

    from src.adapters.firestore_drive_connection_repo import FirestoreDriveConnectionRepository
    from src.adapters.firestore_drive_index_repo import FirestoreDriveIndexRepository
    from src.config.environment import EnvironmentConfig

    env = EnvironmentConfig()
    db = firestore.AsyncClient(project=os.getenv("GOOGLE_CLOUD_PROJECT") or None, database=env.firestore_database_id)
    connections = FirestoreDriveConnectionRepository(db, env.drive_connections_collection,
                                                     env.drive_user_settings_collection)
    conn, why = await resolve_connection(connections, user_id, connection_id)
    if conn is None:
        print(f"refused: {why}")
        return 1
    code, message = await requeue(FirestoreDriveIndexRepository(db, env), conn.scope, reason=reason, kind=kind,
                                  apply=apply, now=datetime.now(timezone.utc), owner=f"requeue-{uuid.uuid4().hex}",
                                  select=select)
    print(message)
    return code


if __name__ == "__main__":
    args = parser().parse_args()
    select, reason = selection_of(args)
    sys.exit(asyncio.run(_main(args.user, args.connection, select, reason, args.kind, args.apply)))

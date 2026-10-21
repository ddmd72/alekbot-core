"""
Orphan-document sweep after a rollback to code that predates §27 (USER_DRIVE_INDEXING_RFC §27.13, §27.15 step 7). Run it
ONLY then: a purge under old code leaves the transcript documents behind (it does not know them), and so does a deleted
sync state. It also deletes LIVE transcript data of the user, so it must never run against a deployment that still has the
transcript stage.

For each user, in this order (the same order and contract as the purge, T-R14):
  1. the user's transcript jobs (with their window results), then their batches, then their stored transcripts
     (`drive_transcript_jobs`, `drive_transcripts`), each until its helper returns 0;
  2. only then the collection-group sweep of window and part documents (`delete_user_transcript_orphans`,
     keep_transcripts=False): it cannot tell an orphan from a live document, so it follows the three helpers. A missing
     composite index (`DriveIndexStoreUnavailable`) is reported as an error naming the sweep and the exit code is 1; re-run
     once the index is READY;
  0. BEFORE any delete, the open reservations are settled exactly as the purge settles them (T-R25): a provider batch
     cannot be cancelled and bills when it finishes, so every open batch's reservation, and a job's with any `submitted`
     window, is BOOKED as transcribe spend in its own month; a job with nothing in flight is only released; then what is
     left in the scanned months' `transcribe_reserved_usd` (`--months`, default 6) is released. Each document is saved
     zeroed before its amount is settled, so a re-run settles nothing twice. The usage documents themselves stay.
  It refuses (exit 1) when the user has records in transcript state ready/waiting/working, unless `--after-rollback`.
The mirror collection (`drive_transcript_mirror`, T2) is not swept by this script: the repository port has no per-user
delete for it yet, and the mirror folder in the user's drive is the user's after a rollback (§27.13).

Dry run by default: lists the users and the open reservations per month, writes nothing (the port cannot count documents).
With --apply it takes each user's lease when a sync state exists (refused while a tick runs: retry in a few minutes).
`--all-users` visits the users that still have a sync state; a user whose state is already gone (an old-code purge) must be
named with `--user` (repeatable).

Usage (project and credentials from the environment / ADC, collections from APP_ENV, default development):
    python scripts/infrastructure/drive_transcript_sweep.py --user <user_id> [--user <user_id> ...] [--months 6] [--apply]
    python scripts/infrastructure/drive_transcript_sweep.py --all-users [--apply]
Exit code: 0 when done (or a dry run), 1 when a lease was busy or the sweep's index is missing.
"""
import argparse
import asyncio
import os
import sys
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.domain.drive_enrichment import UsageClass  # noqa: E402
from src.domain.drive_index import DriveIndexStoreUnavailable  # noqa: E402
from src.domain.drive_transcript_job import SUBMITTED  # noqa: E402
from src.domain.drive_transcript_plan import HELD, PARKED, READY, WAITING, WORKING  # noqa: E402
from src.ports.drive_index_repository import DriveIndexRepository  # noqa: E402

_HOLD = timedelta(minutes=10)
_BATCH = 200
_SCAN = 1000     # records and open batches read for settlement (as the purge)
_MONTHS = 6


def months_back(now: datetime, count: int) -> List[str]:
    """`YYYY-MM` of the current month and the `count - 1` before it."""
    out, year, month = [], now.year, now.month
    for _ in range(max(1, count)):
        out.append(f"{year:04d}-{month:02d}")
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return out


@dataclass
class Settlement:
    booked: float = 0.0       # reservations of audio that may still bill, counted as transcribe spend
    released: float = 0.0     # reservations with nothing in flight, only taken out of the counter


async def _open_reservations(repo: DriveIndexRepository, user_id: str, months: Sequence[str]) -> List[Tuple[str, float]]:
    found = []
    for month in months:
        usage = await repo.get_model_usage(user_id, month)
        if usage.transcribe_reserved_usd > 0:
            found.append((month, usage.transcribe_reserved_usd))
    return found


async def _settle_one(repo: DriveIndexRepository, user_id: str, usd: float, month: str, *, book: bool) -> None:
    """The document was saved zeroed BEFORE this call, so a crash in between never settles twice. `book` counts the money
    as transcribe spend in its own month (the initial build is never touched, G8) and takes it out of the counter."""
    if book:
        await repo.add_model_usage(user_id, month, "", UsageClass.TRANSCRIBE.value, usd, 0.0, 0)
    await repo.reserve_model_usd(user_id, month, UsageClass.TRANSCRIBE.value, -usd)


async def settle_reservations(repo: DriveIndexRepository, user_id: str, months: Sequence[str], *, apply: bool,
                              now: datetime) -> Settlement:
    """T-R25, as the purge does it (`_release_transcript_reservations`), BEFORE any delete: a provider batch cannot be
    cancelled and bills when it finishes, so (1) every open batch's reservation is BOOKED as spend in its own month;
    (2) a job's reservation is booked when any window is `submitted`, released otherwise; (3) what is left in the scanned
    months' counters is released. Each document is saved zeroed first. Without `apply` it only measures."""
    out = Settlement()
    batches = await repo.get_open_batches(user_id, now + timedelta(days=3650), _SCAN)
    item_ids = {key.rsplit(":", 1)[0] for batch in batches for key in batch.request_keys}
    for state in (WORKING, WAITING, PARKED, HELD):
        item_ids.update(rec.item_id for rec in await repo.transcript_records(user_id, state, _SCAN))
    per_month: Dict[str, float] = {}
    for batch in batches:
        if batch.reserved_usd <= 0 or not batch.reserved_month:
            continue
        out.booked += batch.reserved_usd
        per_month[batch.reserved_month] = per_month.get(batch.reserved_month, 0.0) + batch.reserved_usd
        if apply:
            await repo.save_batch(replace(batch, reserved_usd=0.0, reserved_month=""))
            await _settle_one(repo, user_id, batch.reserved_usd, batch.reserved_month, book=True)
    for item_id in sorted(item_ids):
        try:
            job = await repo.get_transcript_job(user_id, item_id)
        except ValueError:
            continue          # unreadable: deleted unsettled, its month counter is released below
        if job is None or job.reserved_usd <= 0 or not job.reserved_month:
            continue
        in_flight = any(w.state == SUBMITTED for w in job.windows)
        if in_flight:
            out.booked += job.reserved_usd
        else:
            out.released += job.reserved_usd
        per_month[job.reserved_month] = per_month.get(job.reserved_month, 0.0) + job.reserved_usd
        if apply:
            await repo.save_transcript_job(replace(job, reserved_usd=0.0, reserved_month=""))
            await _settle_one(repo, user_id, job.reserved_usd, job.reserved_month, book=in_flight)
    for month, counter in await _open_reservations(repo, user_id, months):
        left = counter if apply else max(0.0, counter - per_month.get(month, 0.0))
        if left > 0:
            out.released += left
            if apply:
                await _settle_one(repo, user_id, left, month, book=False)
    return out


async def _drain(deleter, user_id: str) -> int:   # type: ignore[no-untyped-def]
    total = 0
    while True:
        deleted = await deleter(user_id, _BATCH)
        total += deleted
        if deleted == 0:
            return total


async def sweep_user(repo: DriveIndexRepository, user_id: str, *, apply: bool, now: datetime, owner: str,
                     months: int = _MONTHS, after_rollback: bool = False) -> Tuple[int, str]:
    """Returns (exit code, message): counts and dollar amounts only."""
    scan = months_back(now, months)
    live = [state for state in (READY, WAITING, WORKING) if await repo.transcript_records(user_id, state, 1)]
    if live and not after_rollback:
        return 1, (f"refused: records in transcript state {'/'.join(live)} — the transcript stage may still be running. "
                   f"Run this only after the rollback, then pass --after-rollback")
    if not apply:
        plan = await settle_reservations(repo, user_id, scan, apply=False, now=now)
        return 0, (f"dry run: would book ${plan.booked:.4f} in flight, release ${plan.released:.4f}, then delete the "
                   f"transcript jobs, batches, stored transcripts and window/part documents (months scanned: "
                   f"{len(scan)}); re-run with --apply")
    leased = False
    if await repo.get_sync_state(user_id) is not None:
        if await repo.acquire_lease(user_id, owner, now, now + _HOLD, create=False) is None:
            return 1, "a tick holds the lease; retry in a few minutes"
        leased = True
    try:
        settled = await settle_reservations(repo, user_id, scan, apply=True, now=now)   # before any delete
        jobs = await _drain(repo.delete_user_transcript_jobs, user_id)
        batches = await _drain(repo.delete_user_batches, user_id)
        stored = await _drain(repo.delete_user_transcripts, user_id)
        try:   # T-R14: only after the three helpers returned 0
            orphans = await _drain(lambda u, n: repo.delete_user_transcript_orphans(u, n, keep_transcripts=False), user_id)
        except DriveIndexStoreUnavailable:
            return 1, (f"settled (booked ${settled.booked:.4f}, released ${settled.released:.4f}); deleted {jobs} jobs, "
                       f"{batches} batches, {stored} stored transcripts; the window/part sweep needs its "
                       f"collection-group indexes (not READY): re-run once they are")
        remaining = await _open_reservations(repo, user_id, scan)
    finally:
        if leased:
            await repo.release_lease(user_id, owner)
    return 0, (f"booked ${settled.booked:.4f} in flight, released ${settled.released:.4f}; deleted {jobs} jobs, "
               f"{batches} batches, {stored} stored transcripts, {orphans} window/part documents"
               f"{'; reservations still open: ' + str(len(remaining)) if remaining else ''}")


async def run(repo: DriveIndexRepository, user_ids: Sequence[str], *, apply: bool, now: datetime,
              months: int = _MONTHS, after_rollback: bool = False) -> Tuple[int, List[str]]:
    code, lines = 0, []
    for user_id in user_ids:
        user_code, message = await sweep_user(repo, user_id, apply=apply, now=now, owner=f"sweep-{uuid.uuid4().hex}",
                                              months=months, after_rollback=after_rollback)
        code = max(code, user_code)
        lines.append(f"{user_id}: {message}" if len(user_ids) > 1 else message)
    return code, lines


async def _main(users: List[str], all_users: bool, months: int, apply: bool, after_rollback: bool) -> int:
    from google.cloud import firestore

    from src.adapters.firestore_drive_index_repo import FirestoreDriveIndexRepository
    from src.config.environment import EnvironmentConfig

    env = EnvironmentConfig()
    db = firestore.AsyncClient(project=os.getenv("GOOGLE_CLOUD_PROJECT") or None, database=env.firestore_database_id)
    repo = FirestoreDriveIndexRepository(db, env)
    names = sorted(set(users) | (set(await repo.list_sync_user_ids()) if all_users else set()))
    code, lines = await run(repo, names, apply=apply, now=datetime.now(timezone.utc), months=months,
                          after_rollback=after_rollback)
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--user", action="append", default=[], help="a user to sweep (repeatable)")
    parser.add_argument("--all-users", action="store_true", help="every user that still has a sync state")
    parser.add_argument("--months", type=int, default=_MONTHS, help="usage months scanned for open reservations")
    parser.add_argument("--after-rollback", action="store_true",
                        help="confirm the transcript stage is no longer deployed (required when records are in a live state)")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = parser.parse_args()
    if not (args.user or args.all_users):
        parser.error("name a user with --user or pass --all-users")
    sys.exit(asyncio.run(_main(args.user, args.all_users, args.months, args.apply, args.after_rollback)))

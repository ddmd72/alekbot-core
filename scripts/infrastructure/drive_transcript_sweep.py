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
The transcript mirror (T2, §27.9) has three kinds of data, and the default keeps all of them:
  - the root documents (`drive_mirror_roots`) and the ledger (`drive_transcript_mirror`): a roll-FORWARD to T2 needs them
    (without the ledger the mirror would take every file it wrote for a foreign one, §27.9.12). No purge deletes them either;
  - the store entries the ledger pins (an archived or detached transcript, a pending deletion mark; Δ186, Δ214, Δ217): the
    store goes through `delete_user_transcripts_except` with the pinned keys, the same C3 contract as the purge, and the
    window/part sweep then runs in its keep mode (windows only);
  - the mirror folder on the user's drive stays the user's after a rollback (§27.13); this script never touches a drive.
  `--mirror` deletes all three data kinds (the pins are then ignored, the ledger documents and the root documents go after
  the store): use it only to retire the mirror for good. It is a human decision; the dry run lists what it would delete.
  **Precondition of `--mirror --apply` (Δ184, Δ229): the owner has deleted `_Transcripts` or moved it out of the App Folder
  BEFORE any T2 revision runs for that user again.** Opacity of the folder comes from the root documents; once they are gone,
  a T2 revision indexes the folder's files as ordinary documents (paid profiler and passage work) and, with the flag on,
  creates `_Transcripts (Alek)`. So `--mirror --apply` is refused (exit 1) without `--after-rollback`, which confirms it.
  The root documents are deleted by `firestore_root_deleter` in this script, deliberately NOT by a port method: that keeps
  Δ184's "no purge deletes a root document" structural.
  Named residual: a rollback to a T1 image runs T1's orphan sweep (30 days), which does not know the pins and deletes the
  pinned transcripts; the ledger then points at nothing and the next T2 render rebuilds from the record, if it is still there.
  Ledger documents of a drive that has no root document are not found by the per-drive walk; archived ones are caught by the
  user-wide archived query. The rest cannot occur: a ledger document is written only under an existing root document, and only
  this script deletes a root document, last. (The pin reads span drives, so a stray detached or marked document would pin a
  transcript that a re-buy would then keep.)

Per connection since slice 1b (MULTI_DRIVE_RFC §8, §10): transcript jobs, batches and window results belong to a connection
and are swept for each of the user's connections (every state: a disconnected one's leftovers too); the stored transcripts,
the usage and the mirror (roots and ledger by drive identity) are the user's.

Dry run by default: lists the users and the open reservations per month, writes nothing (the port cannot count documents).
With --apply it takes each connection's lease when its sync state exists (refused while a tick runs: retry in a few
minutes). `--all-users` visits the users that still have a sync state on some connection; a user whose states are already
gone must be named with `--user` (repeatable).

Usage (project and credentials from the environment / ADC, collections from APP_ENV, default development):
    python scripts/infrastructure/drive_transcript_sweep.py --user <user_id> [--user <user_id> ...] [--months 6] [--apply]
    python scripts/infrastructure/drive_transcript_sweep.py --all-users [--apply]
    python scripts/infrastructure/drive_transcript_sweep.py --user <user_id> --mirror [--apply]   # also the mirror's data
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
from typing import Any, Awaitable, Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.domain.connection_scope import ConnectionScope  # noqa: E402
from src.domain.drive_enrichment import UsageClass  # noqa: E402
from src.domain.drive_index import DriveIndexStoreUnavailable  # noqa: E402
from src.domain.drive_transcript_job import SUBMITTED  # noqa: E402
from src.domain.drive_transcript_plan import HELD, PARKED, READY, WAITING, WORKING  # noqa: E402
from src.ports.drive_index_repository import DriveIndexRepository  # noqa: E402
from src.ports.drive_mirror_repository import DriveMirrorRepository  # noqa: E402

_HOLD = timedelta(minutes=10)
_BATCH = 200
_SCAN = 1000     # records and open batches read for settlement (as the purge)
_MONTHS = 6
_PAGE = 200

RootDeleter = Callable[[str, str], Awaitable[None]]   # (user_id, drive_identity): the port has no root delete, the script supplies it


def firestore_root_deleter(db: Any, env: Any) -> RootDeleter:
    """Deletes the root document `{env.drive_mirror_roots_collection}/{mirror_root_doc_id(user_id, drive_identity)}`.
    Deliberately not a port method: that keeps Δ184's "no purge deletes a root document" structural (only this script, by
    hand, does)."""
    from src.domain.drive_mirror import mirror_root_doc_id

    async def delete(user_id: str, drive_identity: str) -> None:
        await db.collection(env.drive_mirror_roots_collection).document(
            mirror_root_doc_id(user_id, drive_identity)).delete()
    return delete


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
    no_month: int = 0         # documents holding reserved money but no reserved_month: they cannot be settled by month (N2)


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
                              now: datetime, scopes: Sequence[ConnectionScope] = ()) -> Settlement:
    """T-R25, as the purge does it (`_release_transcript_reservations`), BEFORE any delete: a provider batch cannot be
    cancelled and bills when it finishes, so (1) every open batch's reservation is BOOKED as spend in its own month;
    (2) a job's reservation is booked when any window is `submitted`, released otherwise; (3) what is left in the scanned
    months' counters is released. Each document is saved zeroed first. Without `apply` it only measures. `scopes`: the
    user's connections, whose batches, jobs and records are read; the usage months are the user's."""
    out = Settlement()
    batches = []
    item_ids = set()
    for scope in scopes:
        scope_batches = await repo.get_open_batches(scope, now + timedelta(days=3650), _SCAN)
        batches += scope_batches
        item_ids.update((scope, key.rsplit(":", 1)[0]) for batch in scope_batches for key in batch.request_keys)
        for state in (WORKING, WAITING, PARKED, HELD):
            item_ids.update((scope, rec.item_id) for rec in await repo.transcript_records(scope, state, _SCAN))
    per_month: Dict[str, float] = {}
    for batch in batches:
        if batch.reserved_usd > 0 and not batch.reserved_month:
            out.no_month += 1
        if batch.reserved_usd <= 0 or not batch.reserved_month:
            continue
        out.booked += batch.reserved_usd
        per_month[batch.reserved_month] = per_month.get(batch.reserved_month, 0.0) + batch.reserved_usd
        if apply:
            await repo.save_batch(replace(batch, reserved_usd=0.0, reserved_month=""))
            await _settle_one(repo, user_id, batch.reserved_usd, batch.reserved_month, book=True)
    for scope, item_id in sorted(item_ids, key=lambda pair: (pair[0].connection_id, pair[1])):
        try:
            job = await repo.get_transcript_job(scope, item_id)
        except ValueError:
            continue          # unreadable: deleted unsettled, its month counter is released below
        if job is not None and job.reserved_usd > 0 and not job.reserved_month:
            out.no_month += 1
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


async def _drain(deleter, key: Any) -> int:   # type: ignore[no-untyped-def]
    """Calls `deleter(key, _BATCH)` until it deletes nothing; `key` is the user id or a connection scope."""
    total = 0
    while True:
        deleted = await deleter(key, _BATCH)
        total += deleted
        if deleted == 0:
            return total


def _no_month_note(settlement: Settlement) -> str:
    """N2: money held by a document with no `reserved_month` is not settled by month; say how many there are."""
    if not settlement.no_month:
        return ""
    return f"; {settlement.no_month} documents hold reserved money with no reserved_month (not settled by month)"


async def _drive_docs(mirror_repo: DriveMirrorRepository, user_id: str, drive_identity: str) -> List[str]:
    """Keys of the ledger documents of one drive: the folder/archive walk, then the file walk (both page on the key)."""
    keys: List[str] = []
    for reader in (mirror_repo.folder_docs, mirror_repo.scan_page):
        after: Optional[str] = None
        while True:
            page = await reader(user_id, drive_identity, after, _PAGE)
            keys.extend(doc.key for doc in page)
            if len(page) < _PAGE:
                break
            after = page[-1].key
    return keys


async def mirror_inventory(mirror_repo: DriveMirrorRepository, user_id: str) -> Tuple[List[str], List[str]]:
    """(drive identities of the user's root documents, ledger document keys of those drives). Counts only; no names."""
    drives = [root.drive_identity for root in await mirror_repo.roots(user_id)]
    keys: List[str] = []
    for drive_identity in drives:
        keys.extend(await _drive_docs(mirror_repo, user_id, drive_identity))
    return drives, keys


async def delete_mirror_data(mirror_repo: DriveMirrorRepository, user_id: str, root_deleter: RootDeleter) -> Tuple[int, int]:
    """`--mirror`: the ledger documents first, then the root documents (a root document is what finds the ledger of its
    drive, so a stopped run can be repeated). Returns (documents, roots) deleted."""
    drives, keys = await mirror_inventory(mirror_repo, user_id)
    for key in keys:
        await mirror_repo.delete_doc(user_id, key)
    while True:   # archived documents of a drive with no root document are still reachable by the user-wide query
        archived = await mirror_repo.archived_newest(user_id, _PAGE)
        if not archived:
            break
        for doc in archived:
            await mirror_repo.delete_doc(user_id, doc.key)
            keys.append(doc.key)
    for drive_identity in drives:
        await root_deleter(user_id, drive_identity)
    return len(keys), len(drives)


async def _delete_store(repo: DriveIndexRepository, user_id: str, pins: FrozenSet[str]) -> int:
    """The stored transcripts. Without pins the plain delete (as the purge, before T2); with pins the scanning helper whose
    cursor is held here: a page made only of pinned heads deletes nothing but is not the end (Δ214)."""
    if not pins:
        return await _drain(repo.delete_user_transcripts, user_id)
    total, cursor = 0, None
    while True:
        deleted, cursor = await repo.delete_user_transcripts_except(user_id, _BATCH, pins, cursor)
        total += deleted
        if cursor is None:
            return total


async def sweep_user(repo: DriveIndexRepository, user_id: str, *, apply: bool, now: datetime, owner: str,
                     months: int = _MONTHS, after_rollback: bool = False,
                     mirror_repo: Optional[DriveMirrorRepository] = None, mirror: bool = False,
                     root_deleter: Optional[RootDeleter] = None,
                     scopes: Sequence[ConnectionScope] = ()) -> Tuple[int, str]:
    """Returns (exit code, message): counts and dollar amounts only. With `mirror_repo` the pinned store entries are kept
    (the default) unless `mirror` (`--mirror`) is set, which also needs `root_deleter` to delete the mirror's data.
    `scopes`: the user's connections (their jobs, batches, window results and leases)."""
    if mirror and (mirror_repo is None or root_deleter is None):
        return 1, "--mirror needs the mirror repository and a root deleter"
    if mirror and apply and not after_rollback:
        return 1, ("refused: --mirror --apply deletes the root documents, after which any T2 revision indexes _Transcripts "
                   "as ordinary documents. Delete or move _Transcripts out of the App Folder first, make sure no T2 revision "
                   "runs for this user, then pass --after-rollback")
    pins: FrozenSet[str] = frozenset()
    if mirror_repo is not None and not mirror:
        try:   # before anything is settled or deleted (M2)
            pins = await mirror_repo.pinned_keys(user_id)
        except DriveIndexStoreUnavailable:
            return 1, ("the pin queries failed: the ledger store is unavailable or its single-field indexes are not READY "
                       "(no composite is needed since Δ237): nothing was settled or deleted; re-run once the store answers")
    scan = months_back(now, months)
    live = [state for state in (READY, WAITING, WORKING)
            if any([await repo.transcript_records(scope, state, 1) for scope in scopes])]
    if live and not after_rollback:
        return 1, (f"refused: records in transcript state {'/'.join(live)} — the transcript stage may still be running. "
                   f"Run this only after the rollback, then pass --after-rollback")
    if not apply:
        plan = await settle_reservations(repo, user_id, scan, apply=False, now=now, scopes=scopes)
        mirror_note = ""
        if mirror_repo is not None:
            if mirror:
                drives, keys = await mirror_inventory(mirror_repo, user_id)
                mirror_note = (f"; --mirror: would delete {len(keys)} ledger documents and {len(drives)} root documents, "
                               f"and ignore the pins")
            else:
                mirror_note = (f"; keeping {len(pins)} pinned stored transcripts, the "
                               f"ledger and the root documents (--mirror deletes them)")
        return 0, (f"dry run: would book ${plan.booked:.4f} in flight, release ${plan.released:.4f}, then delete the "
                   f"transcript jobs, batches, stored transcripts and window/part documents (months scanned: "
                   f"{len(scan)}){_no_month_note(plan)}{mirror_note}; re-run with --apply")
    leased: List[ConnectionScope] = []
    try:
        for scope in scopes:
            if await repo.get_sync_state(scope) is not None:
                if await repo.acquire_lease(scope, owner, now, now + _HOLD, create=False) is None:
                    return 1, "a tick holds the lease; retry in a few minutes"
                leased.append(scope)
        settled = await settle_reservations(repo, user_id, scan, apply=True, now=now, scopes=scopes)   # before any delete
        jobs = sum([await _drain(repo.delete_user_transcript_jobs, scope) for scope in scopes])
        batches = sum([await _drain(repo.delete_user_batches, scope) for scope in scopes])
        stored = await _delete_store(repo, user_id, pins)
        try:   # T-R14: only after the three helpers returned 0
            orphans = sum([await _drain(lambda c, n: repo.delete_user_transcript_orphans(c, n, keep_transcripts=bool(pins)),
                                        scope) for scope in scopes])
        except DriveIndexStoreUnavailable:
            return 1, (f"settled (booked ${settled.booked:.4f}, released ${settled.released:.4f}); deleted {jobs} jobs, "
                       f"{batches} batches, {stored} stored transcripts; the window/part sweep needs its "
                       f"collection-group indexes (not READY): re-run once they are")
        remaining = await _open_reservations(repo, user_id, scan)
        mirror_note = ""
        if mirror and mirror_repo is not None and root_deleter is not None:
            docs, roots = await delete_mirror_data(mirror_repo, user_id, root_deleter)
            mirror_note = f"; mirror data deleted: {docs} ledger documents, {roots} root documents"
        elif pins:
            mirror_note = f"; kept {len(pins)} pinned stored transcripts, the ledger and the root documents"
    finally:
        for scope in leased:
            await repo.release_lease(scope, owner)
    return 0, (f"booked ${settled.booked:.4f} in flight, released ${settled.released:.4f}; deleted {jobs} jobs, "
               f"{batches} batches, {stored} stored transcripts, {orphans} window/part documents"
               f"{'; reservations still open: ' + str(len(remaining)) if remaining else ''}{_no_month_note(settled)}{mirror_note}")


async def run(repo: DriveIndexRepository, user_ids: Sequence[str], *, apply: bool, now: datetime,
              months: int = _MONTHS, after_rollback: bool = False, mirror_repo: Optional[DriveMirrorRepository] = None,
              mirror: bool = False, root_deleter: Optional[RootDeleter] = None,
              scopes: Optional[Dict[str, List[ConnectionScope]]] = None) -> Tuple[int, List[str]]:
    """`scopes`: each user's connections (missing: none)."""
    code, lines = 0, []
    for user_id in user_ids:
        user_code, message = await sweep_user(repo, user_id, apply=apply, now=now, owner=f"sweep-{uuid.uuid4().hex}",
                                              months=months, after_rollback=after_rollback, mirror_repo=mirror_repo,
                                              mirror=mirror, root_deleter=root_deleter,
                                              scopes=(scopes or {}).get(user_id, []))
        code = max(code, user_code)
        lines.append(f"{user_id}: {message}" if len(user_ids) > 1 else message)
    return code, lines


async def _main(users: List[str], all_users: bool, months: int, apply: bool, after_rollback: bool,
                mirror: bool = False) -> int:
    from google.cloud import firestore

    from src.adapters.firestore_drive_index_repo import FirestoreDriveIndexRepository
    from src.config.environment import EnvironmentConfig

    env = EnvironmentConfig()
    db = firestore.AsyncClient(project=os.getenv("GOOGLE_CLOUD_PROJECT") or None, database=env.firestore_database_id)
    from src.adapters.firestore_drive_mirror_repo import FirestoreDriveMirrorRepository

    from src.adapters.firestore_drive_connection_repo import FirestoreDriveConnectionRepository

    repo = FirestoreDriveIndexRepository(db, env)
    mirror_repo = FirestoreDriveMirrorRepository(db, env)
    connections = FirestoreDriveConnectionRepository(db, env.drive_connections_collection,
                                                     env.drive_user_settings_collection)
    root_deleter = firestore_root_deleter(db, env)

    synced = await repo.list_sync_scopes()
    names = sorted(set(users) | ({s.user_id for s in synced} if all_users else set()))
    scopes: Dict[str, List[ConnectionScope]] = {}
    for user_id in names:
        found = {c.scope for c in await connections.list_for_user(user_id)} | {s for s in synced if s.user_id == user_id}
        scopes[user_id] = sorted(found, key=lambda s: s.connection_id)
    code, lines = await run(repo, names, apply=apply, now=datetime.now(timezone.utc), months=months,
                            after_rollback=after_rollback, mirror_repo=mirror_repo, mirror=mirror,
                            root_deleter=root_deleter, scopes=scopes)
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--user", action="append", default=[], help="a user to sweep (repeatable)")
    parser.add_argument("--all-users", action="store_true", help="every user that still has a sync state")
    parser.add_argument("--months", type=int, default=_MONTHS, help="usage months scanned for open reservations")
    parser.add_argument("--after-rollback", action="store_true",
                        help="confirm the transcript stage is no longer deployed (required when records are in a live state, "
                             "and for --mirror --apply)")
    parser.add_argument("--mirror", action="store_true",
                        help="also delete the mirror's data: ledger, root documents and the pinned store entries (default: kept). "
                             "--mirror --apply needs --after-rollback: delete or move _Transcripts out of the App Folder "
                             "BEFORE any T2 revision runs for that user again")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run; --apply is for humans only)")
    args = parser.parse_args()
    if not (args.user or args.all_users):
        parser.error("name a user with --user or pass --all-users")
    sys.exit(asyncio.run(_main(args.user, args.all_users, args.months, args.apply, args.after_rollback, args.mirror)))

"""
Rebuild one drive connection's index (USER_DRIVE_INDEXING_RFC §12.2; per connection since MULTI_DRIVE_RFC §8): mark the sync state `purging`, so the next tick
finishes a purge and starts again from epoch 1 — the purge-then-restart path the tick already runs (§7.6). The
answer to "the index looks wrong", and the Search v2 rollout's only migration (D30, §25.8): a v1 index is never
upgraded, it is purged (hint documents, then chunks, then records; the usage and initial-build documents stay) and
re-indexed from scratch under v2. A dry run says whether the index is a v1 one awaiting that rebuild.

`--hints-only` is the Search v2 rollback's step 2 (§25.8): it deletes the user's hint documents
(`{prefix}drive_index_hints`, text derived from their passages) with v2's `delete_user_hints`, before the tag
`drive-search-v1` is deployed — the tag's purge does not know that collection. Records, chunks and the sync state
are left alone (the tag's own rebuild purges them afterwards).

A state that already carries the migration gate key (`MIGRATION_GATE_KEY`) was rebuilt under the current versions:
`--apply` skips it (a repeated `--all-users --apply` does not purge a freshly rebuilt index), unless `--force`; the
dry run says which states would be skipped. `--force` is the answer to "the index looks wrong" on a current index.

A rebuild KEEPS the stored transcripts (RFC §27.8 b, §27.15): `--apply` sets `DriveSyncState.purge_keep_transcripts` in the
same lease-held write as `purging`, so the purge deletes the transcript jobs, batches and window documents (releasing or
booking their reservations) but not the paid-for transcript store, and a full rebuild never re-buys audio. The flag is
read by the purge at each of its steps and dies with the sync state when the purge ends. `--drop-transcripts` clears it
(a purge that also deletes the store). The write is verified by a read-back; a purge that is ALREADY running without the
flag gets it set too (under the lease), but its first steps may have run already: the message says so.

Dry run by default: says what it would do and writes nothing. With --apply it takes the connection's lease (refused while a
tick runs: retry in a few minutes), writes under it and releases it. After a rebuild, the next tick — the fan-out's,
within TICK_INTERVAL, or a continuation already queued — purges and re-onboards the drive.

Usage (project and credentials from the environment / ADC, collections from APP_ENV, default development):
    python scripts/infrastructure/drive_index_rebuild.py --user <user_id> [--connection <id>] [--apply]
    python scripts/infrastructure/drive_index_rebuild.py --all-users [--apply]
    python scripts/infrastructure/drive_index_rebuild.py --user <user_id> --force [--apply]
    python scripts/infrastructure/drive_index_rebuild.py --user <user_id> --drop-transcripts [--apply]
    python scripts/infrastructure/drive_index_rebuild.py --user <user_id> --hints-only [--apply]
    python scripts/infrastructure/drive_index_rebuild.py --all-users --hints-only [--apply]
`--connection` names the connection; without it the user's one live connection is taken (several: refused).
`--all-users` visits every connection that has a sync state.
Exit code: 0 when marked (or a dry run, or a purge already running), 1 when nothing is stored or the lease is busy
(with --all-users: 1 when any connection's step was).
"""
import argparse
import asyncio
import os
import sys
import uuid
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.infrastructure.drive_connection_arg import resolve_connection  # noqa: E402
from src.domain.connection_scope import ConnectionScope  # noqa: E402
from src.domain.drive_index import MIGRATION_GATE_KEY  # noqa: E402
from src.ports.drive_index_repository import DriveIndexRepository  # noqa: E402

_HOLD = timedelta(minutes=5)   # the lease is held for one write; a crash here blocks ticks this long at most
_HINT_PAGE = 500               # hint documents deleted per call (Firestore's batch limit)
_HINT_COUNT_PAGE = 1000        # hint documents counted per page by a dry run


def _v1_note(applied: object) -> str:
    """D30: a state without the migration gate key was built by v1 and is only ever rebuilt."""
    return "" if MIGRATION_GATE_KEY in dict(applied or {}) else " — built under an older index version, awaiting its rebuild (D30)"


def _already_rebuilt(applied: object) -> bool:
    return MIGRATION_GATE_KEY in dict(applied or {})


async def rebuild(repo: DriveIndexRepository, scope: ConnectionScope, *, apply: bool, now: datetime,
                  owner: str, force: bool = False, keep_transcripts: bool = True) -> Tuple[int, str]:
    """Returns (exit code, message). A state already carrying the gate key is skipped (exit 0) unless `force`.
    `keep_transcripts` (default) sets `purge_keep_transcripts` with `purging`, in one write under the lease (§27.8 b)."""
    state = await repo.get_sync_state(scope)
    if state is None:
        return 1, "no sync state: nothing is indexed for this connection"
    if state.purging:
        if not keep_transcripts and state.purge_keep_transcripts:
            return 1, ("a purge is already running in keep-transcripts mode; its mode cannot be changed from here "
                       "(let it finish, then rebuild again with --drop-transcripts)")
        if not (keep_transcripts and not state.purge_keep_transcripts):
            return 0, "a purge is already running; the next tick finishes it and starts epoch 1"
        if not apply:
            return 0, ("dry run: a purge is already running WITHOUT the keep-transcripts flag; --apply would set it "
                       "(the purge's first steps may have run already)")
        return await _set_flags(repo, scope, now=now, owner=owner, keep=True, running=True)
    if _already_rebuilt(state.applied_versions) and not force:
        skipped = "already rebuilt under the current index versions (gate key present); pass --force to rebuild it anyway"
        return 0, (f"dry run: would skip, {skipped}" if not apply else f"skipped: {skipped}")
    if not apply:
        keeps = "keeping the stored transcripts" if keep_transcripts else "deleting the stored transcripts too"
        return 0, (f"dry run: would mark the index purging (epoch {state.epoch}, counters {dict(state.counters)}), {keeps}"
                   f"{_v1_note(state.applied_versions)}; re-run with --apply")
    return await _set_flags(repo, scope, now=now, owner=owner, keep=keep_transcripts, running=False)


async def _set_flags(repo: DriveIndexRepository, scope: ConnectionScope, *, now: datetime, owner: str, keep: bool,
                     running: bool) -> Tuple[int, str]:
    """`purging` and `purge_keep_transcripts` in ONE save, built from the state the lease just returned (never from an
    earlier read), then read back. The race Task 1 carried (a tick saving a state read before this flag was set) cannot
    cross the lease: a tick that holds it makes this refuse (exit 1), and every tick save is by owner and fenced, so a
    tick whose lease ran out writes nothing. What is left is verified, not assumed: a read-back that lacks either flag
    exits 1 and says so."""
    lease = await repo.acquire_lease(scope, owner, now, now + _HOLD, create=False)
    if lease is None:
        return 1, "a tick holds the lease (or the state just went away); retry in a few minutes"
    try:
        await repo.save_sync_state(replace(lease, purging=True, purge_keep_transcripts=keep), owner)
        after = await repo.get_sync_state(scope)   # read back while the lease is still ours (M4)
    finally:
        await repo.release_lease(scope, owner)
    if after is None or not after.purging or after.purge_keep_transcripts != keep:
        return 1, "the purge flags did not stick (the state changed under the write); re-run, or check the state"
    kept = "the stored transcripts are kept" if keep else "the stored transcripts are deleted with it"
    if running:
        return 0, (f"keep-transcripts flag set on the running purge; its first steps may have run already, so parts of "
                    f"transcripts it already deleted may be orphaned (a later sweep removes them); {kept}")
    return 0, f"marked purging: the next tick purges the index and rebuilds it from epoch 1; {kept}"


async def _count_hints(repo: DriveIndexRepository, scope: ConnectionScope) -> int:
    count, after = 0, None
    while True:
        page = await repo.hints_page(scope, after, _HINT_COUNT_PAGE)
        count += len(page)
        if len(page) < _HINT_COUNT_PAGE:
            return count
        last = page[-1]
        after = (last.item_id, last.generation, last.index)


async def delete_hints(repo: DriveIndexRepository, scope: ConnectionScope, *, apply: bool, now: datetime,
                       owner: str) -> Tuple[int, str]:
    """Rollback step 2 (§25.8): every hint document of the connection, under the lease when a sync state exists (none left
    means no tick to race). Returns (exit code, message)."""
    if not apply:
        return 0, f"dry run: would delete {await _count_hints(repo, scope)} hint documents; re-run with --apply"
    state = await repo.get_sync_state(scope)
    leased = False
    if state is not None:
        if await repo.acquire_lease(scope, owner, now, now + _HOLD, create=False) is None:
            return 1, "a tick holds the lease; pause the drive queue first, then retry"
        leased = True
    deleted = 0
    try:
        while True:
            n = await repo.delete_user_hints(scope, _HINT_PAGE)
            deleted += n
            if n < _HINT_PAGE:
                break
    finally:
        if leased:
            await repo.release_lease(scope, owner)
    return 0, f"deleted {deleted} hint documents"


async def run(repo: DriveIndexRepository, scopes: List[ConnectionScope], *, hints_only: bool, apply: bool,
              now: datetime, force: bool = False, keep_transcripts: bool = True) -> Tuple[int, List[str]]:
    """The step for each connection, in order; the worst exit code and one line per connection, prefixed when several:
    by the user, or by `user/connection` for a user with more than one connection in the run."""
    code, lines = 0, []
    step = delete_hints if hints_only else rebuild
    per_user = Counter(scope.user_id for scope in scopes)
    for scope in scopes:
        extra = {} if hints_only else {"force": force, "keep_transcripts": keep_transcripts}
        user_code, message = await step(repo, scope, apply=apply, now=now, owner=f"rebuild-{uuid.uuid4().hex}",
                                        **extra)
        code = max(code, user_code)
        label = scope.user_id if per_user[scope.user_id] == 1 else f"{scope.user_id}/{scope.connection_id}"
        lines.append(f"{label}: {message}" if len(scopes) > 1 else message)
    return code, lines


async def _main(user_id: Optional[str], connection_id: Optional[str], all_users: bool, hints_only: bool, apply: bool,
                force: bool = False, keep_transcripts: bool = True) -> int:
    from google.cloud import firestore

    from src.adapters.firestore_drive_connection_repo import FirestoreDriveConnectionRepository
    from src.adapters.firestore_drive_index_repo import FirestoreDriveIndexRepository
    from src.config.environment import EnvironmentConfig

    env = EnvironmentConfig()
    db = firestore.AsyncClient(project=os.getenv("GOOGLE_CLOUD_PROJECT") or None, database=env.firestore_database_id)
    repo = FirestoreDriveIndexRepository(db, env)
    if all_users:
        scopes = sorted(await repo.list_sync_scopes(), key=lambda s: (s.user_id, s.connection_id))
    else:
        connections = FirestoreDriveConnectionRepository(db, env.drive_connections_collection,
                                                         env.drive_user_settings_collection)
        conn, why = await resolve_connection(connections, str(user_id), connection_id)
        if conn is None:
            print(f"refused: {why}")
            return 1
        scopes = [conn.scope]
    code, lines = await run(repo, scopes, hints_only=hints_only, apply=apply, force=force,
                            keep_transcripts=keep_transcripts, now=datetime.now(timezone.utc))
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--user", help="user whose connection's index is rebuilt")
    who.add_argument("--all-users", action="store_true", help="every connection with a sync state")
    parser.add_argument("--connection", default=None, help="the connection (default: the user's one live connection)")
    parser.add_argument("--hints-only", action="store_true",
                        help="delete the connection's hint documents only (Search v2 rollback, step 2)")
    parser.add_argument("--force", action="store_true",
                        help="rebuild a state that already carries the current versions (default: skipped)")
    parser.add_argument("--drop-transcripts", action="store_true",
                        help="the purge deletes the stored transcripts too (default: a rebuild keeps them, §27.8 b)")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args.user, args.connection, args.all_users, args.hints_only, args.apply, args.force,
                               not args.drop_transcripts)))

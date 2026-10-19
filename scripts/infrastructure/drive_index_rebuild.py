"""
Rebuild one user's drive index (USER_DRIVE_INDEXING_RFC §12.2): mark the sync state `purging`, so the next tick
finishes a purge and starts again from epoch 1 — the purge-then-restart path the tick already runs (§7.6). The
answer to "the index looks wrong", and the Search v2 rollout's only migration (D30, §25.8): a v1 index is never
upgraded, it is purged (hint documents, then chunks, then records; the usage and initial-build documents stay) and
re-indexed from scratch under v2. A dry run says whether the index is a v1 one awaiting that rebuild.

`--hints-only` is the Search v2 rollback's step 2 (§25.8): it deletes the user's hint documents
(`{prefix}drive_index_hints`, text derived from their passages) with v2's `delete_user_hints`, before the tag
`drive-search-v1` is deployed — the tag's purge does not know that collection. Records, chunks and the sync state
are left alone (the tag's own rebuild purges them afterwards).

Dry run by default: says what it would do and writes nothing. With --apply it takes the user's lease (refused while a
tick runs: retry in a few minutes), writes under it and releases it. After a rebuild, the next tick — the fan-out's,
within TICK_INTERVAL, or a continuation already queued — purges and re-onboards the drive.

Usage (project and credentials from the environment / ADC, collections from APP_ENV, default development):
    python scripts/infrastructure/drive_index_rebuild.py --user <user_id> [--apply]
    python scripts/infrastructure/drive_index_rebuild.py --all-users [--apply]
    python scripts/infrastructure/drive_index_rebuild.py --user <user_id> --hints-only [--apply]
    python scripts/infrastructure/drive_index_rebuild.py --all-users --hints-only [--apply]
Exit code: 0 when marked (or a dry run, or a purge already running), 1 when nothing is stored or the lease is busy
(with --all-users: 1 when any user's step was).
"""
import argparse
import asyncio
import os
import sys
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.domain.drive_index import MIGRATION_GATE_KEY  # noqa: E402
from src.ports.drive_index_repository import DriveIndexRepository  # noqa: E402

_HOLD = timedelta(minutes=5)   # the lease is held for one write; a crash here blocks ticks this long at most
_HINT_PAGE = 500               # hint documents deleted per call (Firestore's batch limit)
_HINT_COUNT_PAGE = 1000        # hint documents counted per page by a dry run


def _v1_note(applied: object) -> str:
    """D30: a state without the migration gate key was built by v1 and is only ever rebuilt."""
    return "" if MIGRATION_GATE_KEY in dict(applied or {}) else " — built under an older index version, awaiting its rebuild (D30)"


async def rebuild(repo: DriveIndexRepository, user_id: str, *, apply: bool, now: datetime,
                  owner: str) -> Tuple[int, str]:
    """Returns (exit code, message)."""
    state = await repo.get_sync_state(user_id)
    if state is None:
        return 1, "no sync state: nothing is indexed for this user"
    if state.purging:
        return 0, "a purge is already running; the next tick finishes it and starts epoch 1"
    if not apply:
        return 0, (f"dry run: would mark the index purging (epoch {state.epoch}, counters {dict(state.counters)})"
                   f"{_v1_note(state.applied_versions)}; re-run with --apply")
    lease = await repo.acquire_lease(user_id, owner, now, now + _HOLD, create=False)
    if lease is None:
        return 1, "a tick holds the lease (or the state just went away); retry in a few minutes"
    try:
        await repo.save_sync_state(replace(lease, purging=True), owner)
    finally:
        await repo.release_lease(user_id, owner)
    return 0, "marked purging: the next tick purges the index and rebuilds it from epoch 1"


async def _count_hints(repo: DriveIndexRepository, user_id: str) -> int:
    count, after = 0, None
    while True:
        page = await repo.hints_page(user_id, after, _HINT_COUNT_PAGE)
        count += len(page)
        if len(page) < _HINT_COUNT_PAGE:
            return count
        last = page[-1]
        after = (last.item_id, last.generation, last.index)


async def delete_hints(repo: DriveIndexRepository, user_id: str, *, apply: bool, now: datetime,
                       owner: str) -> Tuple[int, str]:
    """Rollback step 2 (§25.8): every hint document of the user, under the lease when a sync state exists (none left
    means no tick to race). Returns (exit code, message)."""
    if not apply:
        return 0, f"dry run: would delete {await _count_hints(repo, user_id)} hint documents; re-run with --apply"
    state = await repo.get_sync_state(user_id)
    leased = False
    if state is not None:
        if await repo.acquire_lease(user_id, owner, now, now + _HOLD, create=False) is None:
            return 1, "a tick holds the lease; pause the drive queue first, then retry"
        leased = True
    deleted = 0
    try:
        while True:
            n = await repo.delete_user_hints(user_id, _HINT_PAGE)
            deleted += n
            if n < _HINT_PAGE:
                break
    finally:
        if leased:
            await repo.release_lease(user_id, owner)
    return 0, f"deleted {deleted} hint documents"


async def run(repo: DriveIndexRepository, user_ids: List[str], *, hints_only: bool, apply: bool,
              now: datetime) -> Tuple[int, List[str]]:
    """The step for each user, in order; the worst exit code and one line per user (prefixed when several)."""
    code, lines = 0, []
    step = delete_hints if hints_only else rebuild
    for user_id in user_ids:
        user_code, message = await step(repo, user_id, apply=apply, now=now, owner=f"rebuild-{uuid.uuid4().hex}")
        code = max(code, user_code)
        lines.append(f"{user_id}: {message}" if len(user_ids) > 1 else message)
    return code, lines


async def _main(user_id: Optional[str], all_users: bool, hints_only: bool, apply: bool) -> int:
    from google.cloud import firestore

    from src.adapters.firestore_drive_index_repo import FirestoreDriveIndexRepository
    from src.config.environment import EnvironmentConfig

    env = EnvironmentConfig()
    db = firestore.AsyncClient(project=os.getenv("GOOGLE_CLOUD_PROJECT") or None, database=env.firestore_database_id)
    repo = FirestoreDriveIndexRepository(db, env)
    users = sorted(await repo.list_sync_user_ids()) if all_users else [str(user_id)]
    code, lines = await run(repo, users, hints_only=hints_only, apply=apply, now=datetime.now(timezone.utc))
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--user", help="user whose index is rebuilt")
    who.add_argument("--all-users", action="store_true", help="every user with a sync state")
    parser.add_argument("--hints-only", action="store_true",
                        help="delete the user's hint documents only (Search v2 rollback, step 2)")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args.user, args.all_users, args.hints_only, args.apply)))

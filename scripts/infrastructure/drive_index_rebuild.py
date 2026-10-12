"""
Rebuild one user's drive index (USER_DRIVE_INDEXING_RFC §12.2): mark the sync state `purging`, so the next tick
finishes a purge and starts again from epoch 1 — the purge-then-restart path the tick already runs (§7.6). The
answer to "the index looks wrong"; not the version-migration path (§12.3).

Dry run by default: says what it would do and writes nothing. With --apply it takes the user's lease (refused while a
tick runs: retry in a few minutes), writes `purging = True` under it and releases it. The next tick — the fan-out's,
within TICK_INTERVAL, or a continuation already queued — purges and re-onboards the drive.

Usage (project and credentials from the environment / ADC, collections from APP_ENV, default development):
    python scripts/infrastructure/drive_index_rebuild.py --user <user_id> [--apply]
Exit code: 0 when marked (or a dry run, or a purge already running), 1 when nothing is stored or the lease is busy.
"""
import argparse
import asyncio
import os
import sys
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.ports.drive_index_repository import DriveIndexRepository  # noqa: E402

_HOLD = timedelta(minutes=5)   # the lease is held for one write; a crash here blocks ticks this long at most


async def rebuild(repo: DriveIndexRepository, user_id: str, *, apply: bool, now: datetime,
                  owner: str) -> Tuple[int, str]:
    """Returns (exit code, message)."""
    state = await repo.get_sync_state(user_id)
    if state is None:
        return 1, "no sync state: nothing is indexed for this user"
    if state.purging:
        return 0, "a purge is already running; the next tick finishes it and starts epoch 1"
    if not apply:
        return 0, (f"dry run: would mark the index purging (epoch {state.epoch}, counters {dict(state.counters)}); "
                   f"re-run with --apply")
    lease = await repo.acquire_lease(user_id, owner, now, now + _HOLD, create=False)
    if lease is None:
        return 1, "a tick holds the lease (or the state just went away); retry in a few minutes"
    try:
        await repo.save_sync_state(replace(lease, purging=True), owner)
    finally:
        await repo.release_lease(user_id, owner)
    return 0, "marked purging: the next tick purges the index and rebuilds it from epoch 1"


async def _main(user_id: str, apply: bool) -> int:
    from google.cloud import firestore

    from src.adapters.firestore_drive_index_repo import FirestoreDriveIndexRepository
    from src.config.environment import EnvironmentConfig

    env = EnvironmentConfig()
    db = firestore.AsyncClient(project=os.getenv("GOOGLE_CLOUD_PROJECT") or None, database=env.firestore_database_id)
    code, message = await rebuild(FirestoreDriveIndexRepository(db, env), user_id, apply=apply,
                                  now=datetime.now(timezone.utc), owner=f"rebuild-{uuid.uuid4().hex}")
    print(message)
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--user", required=True, help="user whose index is rebuilt")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args.user, args.apply)))

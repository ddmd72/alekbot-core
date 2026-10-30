"""
Turn drive transcription on or off for ONE drive connection (USER_DRIVE_INDEXING_RFC §27.15 step 4, Δ125, G29;
MULTI_DRIVE_RFC §4.4, §12 step 5): sets the connection's `transcribe_enabled` (the per-user
`UserBotConfig.drive_transcribe_enabled` moved to the connection in slice 1b) through the connection repository.
Transcription runs only with the global kill switch (`DRIVE_TRANSCRIBE_ENABLED`, the indexer's environment) AND this
switch, so this is the consent step: the owner runs it, with `--apply`, for their own connection. `--connection` names
it; without it the user's one live connection is taken (several: refused with the list).

Dry run by default: prints the switch's current value and what it would become, and writes nothing. With --apply it writes
and prints before and after (the value read back from the store). Turning it off stops new work at the next tick; jobs, the
transcript store and the mirror stay (§27.13 rollback levers).

Usage (project and credentials from the environment / ADC, collections from APP_ENV, default development):
    python scripts/infrastructure/drive_transcribe_flag.py --user <user_id> [--connection <id>] [--off] [--apply]
Exit code: 0 on success (or a dry run), 1 when the connection is not found (or not live) or the write did not stick.
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.infrastructure.drive_connection_arg import resolve_connection  # noqa: E402
from src.ports.drive_connection_repository import DriveConnectionRepository  # noqa: E402


async def set_flag(connections: DriveConnectionRepository, user_id: str, *, enabled: bool, apply: bool,
                   connection_id: Optional[str] = None) -> Tuple[int, str]:
    """Returns (exit code, message). Prints nothing, logs nothing: the message carries the before/after values only."""
    conn, why = await resolve_connection(connections, user_id, connection_id)
    if conn is None:
        return 1, why
    name = f"transcribe_enabled of connection {conn.connection_id}"
    before = conn.transcribe_enabled
    if not apply:
        return 0, f"dry run: {name} is {before}, would become {enabled}; re-run with --apply"
    if before == enabled:
        return 0, f"{name} is already {before}; nothing written"
    await connections.update(user_id, conn.connection_id, transcribe_enabled=enabled)
    reread = await connections.get(user_id, conn.connection_id)
    after = None if reread is None else reread.transcribe_enabled
    if after != enabled:
        return 1, f"{name}: before {before}, after {after} — the write did not stick"
    return 0, f"{name}: before {before}, after {after}"


async def _main(user_id: str, connection_id: Optional[str], enabled: bool, apply: bool) -> int:
    from google.cloud import firestore

    from src.adapters.firestore_drive_connection_repo import FirestoreDriveConnectionRepository
    from src.config.environment import EnvironmentConfig

    env = EnvironmentConfig()
    db = firestore.AsyncClient(project=os.getenv("GOOGLE_CLOUD_PROJECT") or None, database=env.firestore_database_id)
    connections = FirestoreDriveConnectionRepository(db, env.drive_connections_collection,
                                                     env.drive_user_settings_collection)
    code, message = await set_flag(connections, user_id, enabled=enabled, apply=apply, connection_id=connection_id)
    print(message)
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--user", required=True, help="the user whose connection's switch is set")
    parser.add_argument("--connection", default=None, help="the connection (default: the user's one live connection)")
    parser.add_argument("--off", action="store_true", help="turn it off (default: on)")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args.user, args.connection, not args.off, args.apply)))

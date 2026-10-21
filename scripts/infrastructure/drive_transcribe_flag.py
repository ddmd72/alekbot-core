"""
Turn drive transcription on or off for ONE user (USER_DRIVE_INDEXING_RFC §27.15 step 4, Δ125, G29): sets
`UserBotConfig.drive_transcribe_enabled` through the user repository, the same read-modify-write path the Cabinet uses for
`drive_indexing_enabled`. Transcription runs only with the global kill switch (`DRIVE_TRANSCRIBE_ENABLED`, the indexer's
environment) AND this per-user flag, so this is the consent step: the owner runs it, with `--apply`, for their own user.

Dry run by default: prints the flag's current value and what it would become, and writes nothing. With --apply it writes and
prints before and after (the value read back from the store). Turning it off stops new work at the next tick; jobs, the
transcript store and the mirror stay (§27.13 rollback levers).

Usage (project and credentials from the environment / ADC, collections from APP_ENV, default development):
    python scripts/infrastructure/drive_transcribe_flag.py --user <user_id> [--off] [--apply]
Exit code: 0 on success (or a dry run), 1 when the user does not exist or the write did not stick.
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.ports.user_repository import UserRepository  # noqa: E402


async def set_flag(users: UserRepository, user_id: str, *, enabled: bool, apply: bool) -> Tuple[int, str]:
    """Returns (exit code, message). Prints nothing, logs nothing: the message carries the before/after values only."""
    user = await users.get_user(user_id)
    if user is None:
        return 1, "no such user"
    before = bool(user.config.drive_transcribe_enabled)
    if not apply:
        return 0, f"dry run: drive_transcribe_enabled is {before}, would become {enabled}; re-run with --apply"
    if before == enabled:
        return 0, f"drive_transcribe_enabled is already {before}; nothing written"
    user.config.drive_transcribe_enabled = enabled
    await users.update_user(user)
    reread = await users.get_user(user_id)
    after = None if reread is None else bool(reread.config.drive_transcribe_enabled)
    if after != enabled:
        return 1, f"drive_transcribe_enabled: before {before}, after {after} — the write did not stick"
    return 0, f"drive_transcribe_enabled: before {before}, after {after}"


async def _main(user_id: str, enabled: bool, apply: bool) -> int:
    from google.cloud import firestore

    from src.adapters.firestore_account_repo import FirestoreAccountRepository
    from src.adapters.firestore_user_repo import FirestoreUserRepository
    from src.config.environment import EnvironmentConfig

    env = EnvironmentConfig()
    db = firestore.AsyncClient(project=os.getenv("GOOGLE_CLOUD_PROJECT") or None, database=env.firestore_database_id)
    accounts = FirestoreAccountRepository(db_client=db, collection_name=env.account_collection_name)
    code, message = await set_flag(FirestoreUserRepository(db, env, accounts), user_id, enabled=enabled, apply=apply)
    print(message)
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--user", required=True, help="the user whose flag is set")
    parser.add_argument("--off", action="store_true", help="turn it off (default: on)")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args.user, not args.off, args.apply)))

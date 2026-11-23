"""Switch the external task queue on or off for one user (EXTERNAL_TASK_QUEUE_RFC §5.10, D12).

The flag `config.external_tasks_enabled` is set only by an operator, by this script or the console: no Cabinet route, no
`$` command and no API writes it. The script writes that one field through `FirestoreUserRepository.update_user_fields`,
so nothing else on the user document is round-tripped.

    python -m scripts.set_external_tasks_flag <user_id>                  # dry run: prints current and target
    python -m scripts.set_external_tasks_flag <user_id> --apply          # switch on
    python -m scripts.set_external_tasks_flag <user_id> --off --apply    # switch off

Without `--apply` nothing is written. Smart's tools follow the flag within about an hour (the agent factory caches a
profile that long); the service itself reads the flag fresh on every call. Switching off deletes nothing: existing
tasks run to their deadlines without being served. Only the first 8 characters of the id are printed.
"""
import argparse
import asyncio
import sys
from typing import Any, Callable

from scripts._firestore import async_client
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.config.environment import EnvironmentConfig
from src.ports.user_repository import UserRepository

FIELD = "config.external_tasks_enabled"


async def set_flag(user_repo: UserRepository, user_id: str, *, enabled: bool, apply: bool,
                   out: Callable[[str], Any] = print) -> int:
    """Exit code: 0 done (or dry run), 1 unknown user."""
    who = user_id[:8]
    user = await user_repo.get_user(user_id)
    if user is None:
        out(f"user {who} not found; nothing written")
        return 1
    current = user.config.external_tasks_enabled
    out(f"user {who}: external_tasks_enabled is {current}, target {enabled}")
    if not apply:
        out("dry run: nothing written; pass --apply to write")
        return 0
    user.config.external_tasks_enabled = enabled
    await user_repo.update_user_fields(user, [FIELD])
    out(f"user {who}: external_tasks_enabled set to {enabled}")
    out("Smart's tools follow within about an hour (the agent profile cache); the service refuses or serves at once")
    return 0


async def _main(user_id: str, enabled: bool, apply: bool) -> int:
    env_config = EnvironmentConfig()
    db = async_client()
    account_repo = FirestoreAccountRepository(db_client=db, collection_name=env_config.account_collection_name)
    return await set_flag(FirestoreUserRepository(db, env_config, account_repo), user_id, enabled=enabled,
                          apply=apply)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("user_id")
    parser.add_argument("--off", action="store_true", help="switch the queue off (default: on)")
    parser.add_argument("--apply", action="store_true", help="write; without it the script only prints")
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args.user_id, not args.off, args.apply)))

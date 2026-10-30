"""
Reset one user's Search v2 initial build (USER_DRIVE_INDEXING_RFC §25.4.5, D27): the one-time allowance spent first,
before the monthly caps, on the first full description of a drive. Once closed it never reopens — not by a purge, a
rebuild or a toggle; only this owner script writes an unopened build again (spent 0, no open or close time), so the
next onboarding or rebuild spends a fresh allowance.

When to run it: after a rebuild whose description should be paid by a new allowance (a failed first build, a
re-index after the profiler prompt changed). The build is per provider account since slice 1b (MULTI_DRIVE_RFC §10,
D8): the record is `{prefix}drive_model_usage/{initial_build_doc_id(user, realm, subject)}`, the account of the
connection's credential (`--connection`, or the user's one live connection); the monthly usage documents are left
alone. The allowance defaults to INITIAL_BUILD_USD ($40) and is never under the owner's $20 minimum.

Dry run by default. Run it while no tick spends for the user (pause the drive queue, or between ticks): a tick's
transactional spend that commits after the reset still counts against the new allowance, which is the safe direction.

Usage (project and credentials from the environment / ADC, collections from APP_ENV, default development):
    python scripts/infrastructure/drive_initial_build_reset.py --user <user_id> [--connection <id>] [--allowance-usd 40] [--apply]
Exit code: 0 when written (or a dry run), 1 when refused.
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Optional, Protocol, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.infrastructure.drive_connection_arg import account_of, resolve_connection  # noqa: E402
from src.domain.drive_enrichment import InitialBuild  # noqa: E402
from src.domain.drive_index_params import INITIAL_BUILD_USD  # noqa: E402
from src.domain.drive_usage import initial_build_doc_id  # noqa: E402
from src.ports.drive_connection_repository import DriveConnectionRepository  # noqa: E402

OWNER_MINIMUM_USD = 20.0   # D27: the owner's floor for an initial build


class BuildStore(Protocol):
    """The initial-build record of one account; `key` is its document id (`initial_build_doc_id`)."""

    async def get(self, key: str) -> Optional[InitialBuild]: ...

    async def put(self, key: str, build: InitialBuild) -> None: ...


def _describe(build: Optional[InitialBuild]) -> str:
    if build is None:
        return "no initial build stored (an unopened one applies)"
    state = "closed" if build.closed_at else ("open" if build.opened_at else "unopened")
    return f"{state}, spent ${build.spent_usd:.2f} of ${build.allowance_usd:.2f}"


async def reset(store: BuildStore, key: str, *, allowance_usd: Optional[float], apply: bool) -> Tuple[int, str]:
    """`key`: the account's initial-build document id (`build_key`)."""
    allowance = INITIAL_BUILD_USD if allowance_usd is None else float(allowance_usd)
    if allowance < OWNER_MINIMUM_USD:
        return 1, f"refused: the allowance must be at least ${OWNER_MINIMUM_USD:.0f} (D27), not ${allowance:.2f}"
    before = await store.get(key)
    if not apply:
        return 0, (f"dry run: now {_describe(before)}; would write an unopened build of ${allowance:.2f}; "
                   f"re-run with --apply")
    await store.put(key, InitialBuild(allowance_usd=allowance, spent_usd=0.0, opened_at=None, closed_at=None))
    return 0, f"reset: was {_describe(before)}; now unopened, ${allowance:.2f}"


async def build_key(connections: DriveConnectionRepository, db: Any, credentials_collection: str, user_id: str,
                    connection_id: Optional[str]) -> Tuple[Optional[str], str]:
    """(the initial-build document id of the connection's provider account, "") or (None, why it was refused)."""
    conn, why = await resolve_connection(connections, user_id, connection_id)
    if conn is None:
        return None, why
    realm, subject = await account_of(db, credentials_collection, user_id, conn.credential_id)
    if not realm or not subject:
        return None, "the connection's credential is gone or has no account"
    return initial_build_doc_id(user_id, realm, subject), ""


class FirestoreBuildStore:
    """One user's initial-build usage documents, through the repository's own codec."""

    def __init__(self, db: Any, collection: str, user_id: str) -> None:
        self._col = db.collection(collection)
        self._user_id = user_id

    async def get(self, key: str) -> Optional[InitialBuild]:
        from src.adapters.firestore_drive_index_repo import initial_from_doc

        snap = await self._col.document(key).get()
        return initial_from_doc(snap.to_dict()) if snap.exists else None

    async def put(self, key: str, build: InitialBuild) -> None:
        from src.adapters.firestore_drive_index_repo import initial_to_doc

        await self._col.document(key).set(initial_to_doc(self._user_id, build))


async def _main(user_id: str, connection_id: Optional[str], allowance_usd: Optional[float], apply: bool) -> int:
    from google.cloud import firestore

    from src.adapters.firestore_drive_connection_repo import FirestoreDriveConnectionRepository
    from src.config.environment import EnvironmentConfig

    env = EnvironmentConfig()
    db = firestore.AsyncClient(project=os.getenv("GOOGLE_CLOUD_PROJECT") or None, database=env.firestore_database_id)
    connections = FirestoreDriveConnectionRepository(db, env.drive_connections_collection,
                                                     env.drive_user_settings_collection)
    key, why = await build_key(connections, db, env.credentials_collection, user_id, connection_id)
    if key is None:
        print(f"refused: {why}")
        return 1
    code, message = await reset(FirestoreBuildStore(db, env.drive_model_usage_collection, user_id), key,
                                allowance_usd=allowance_usd, apply=apply)
    print(message)
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--user", required=True, help="user whose initial build is reset")
    parser.add_argument("--connection", default=None,
                        help="the connection whose account's build is reset (default: the user's one live connection)")
    parser.add_argument("--allowance-usd", type=float, default=None,
                        help=f"the new allowance (default {INITIAL_BUILD_USD:.0f}, at least {OWNER_MINIMUM_USD:.0f})")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args.user, args.connection, args.allowance_usd, args.apply)))

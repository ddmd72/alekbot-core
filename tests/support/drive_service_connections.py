"""Drive connections for tests of the drive service's callers (DriveOperations, FileManagementAgent, the drive tools),
MULTI_DRIVE_RFC §6, §9: technical ids only.

- `seeded_connections(*user_ids, tag=REF_TAG)`: the production `DriveConnectionService` over the in-memory repository,
  each user with one ACTIVE primary connection `conn-1` tagged `tag`, seeded synchronously (for a sync fixture or a
  constructor argument of `UserDriveService(drive, connections)`).
- `ConnectionReads`: a mixin for a hand fake of `UserDriveService` that gives it the service's connection reads
  (`status`, `primary`, `resolve_ref`) by running the production `UserDriveService` methods over
  `seeded_connections`. Any user that asks has its one connection (seeded on first read), so `drive:a1:<item>`
  resolves and an untagged `drive:<item>` answers the stale-ref line (D13).
- `ref(item_id, tag=REF_TAG)`: the tagged ref the code prints for an item of that connection.
"""
from __future__ import annotations

from typing import Any, Optional

from src.domain import drive_ref
from src.domain.drive_connection import DriveConnection
from src.domain.drive_connection_status import DriveConnectionStatus
from src.domain.drive_resolved_ref import ResolvedDriveRef
from src.domain.drive_user_settings import DriveUserSettings
from src.services.drive_connection_service import DriveConnectionService
from src.services.user_drive_service import UserDriveService

from .drive_connection_repo import FakeDriveConnectionRepository
from .drive_connection_service_fake import connection

REF_TAG = "a1"
CONNECTION_ID = "conn-1"


def ref(item_id: str, tag: str = REF_TAG) -> str:
    """`drive:<tag>:<item_id>`."""
    return drive_ref.format(tag, item_id)


def seed_user(repo: FakeDriveConnectionRepository, user_id: str, *, tag: str = REF_TAG,
              connection_id: str = CONNECTION_ID) -> DriveConnection:
    """Store the user's one ACTIVE connection and point the primary at it (synchronously); returns it."""
    conn = connection(user_id, connection_id, ref_tag=tag, drive_identity=f"fake_a:{connection_id}",
                      credential_id=f"cred-{connection_id}")
    repo.connections[(user_id, connection_id)] = conn
    repo.settings[user_id] = DriveUserSettings(user_id=user_id, primary_connection_id=connection_id)
    return conn


def seeded_connections(*user_ids: str, tag: str = REF_TAG,
                       repo: Optional[FakeDriveConnectionRepository] = None) -> DriveConnectionService:
    """The production DriveConnectionService; each of `user_ids` has one ACTIVE primary connection tagged `tag`."""
    repo = repo if repo is not None else FakeDriveConnectionRepository()
    for user_id in user_ids:
        seed_user(repo, user_id, tag=tag)
    return DriveConnectionService(repo, None)   # type: ignore[arg-type]   # credentials: read by flag_reconnect only


class ConnectionReads:
    """Mixin for a hand fake of UserDriveService: `status`, `primary`, `resolve_ref` as the service answers them, over
    one ACTIVE primary connection (tag `ref_tag`) per user, seeded on the user's first read."""

    ref_tag: str = REF_TAG

    def _reads(self, user_id: str) -> UserDriveService:
        repo: Optional[FakeDriveConnectionRepository] = getattr(self, "_reads_repo", None)
        if repo is None:
            repo = FakeDriveConnectionRepository()
            self._reads_repo = repo
        if (user_id, CONNECTION_ID) not in repo.connections:
            seed_user(repo, user_id, tag=self.ref_tag)
        return UserDriveService(drive=None, connections=DriveConnectionService(repo, None))  # type: ignore[arg-type]

    async def status(self, user_id: str) -> DriveConnectionStatus:
        return await self._reads(user_id).status(user_id)

    async def primary(self, user_id: str) -> DriveConnection:
        return await self._reads(user_id).primary(user_id)

    async def resolve_ref(self, user_id: str, ref: str) -> ResolvedDriveRef:
        return await self._reads(user_id).resolve_ref(user_id, ref)


def connection_of(service: Any, user_id: str) -> DriveConnection:
    """The connection `ConnectionReads` seeded for `user_id` (after its first read)."""
    return service._reads_repo.connections[(user_id, CONNECTION_ID)]

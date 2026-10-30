"""FakeDriveConnectionService — the part of `DriveConnectionService` the drive indexer and the fan-out read, with the
service's rules, over `FakeDriveConnectionRepository` (MULTI_DRIVE_RFC §4.4, §8).

A fake with semantics: `flag_reconnect` moves ACTIVE -> NEEDS_RECONNECT through the repository's `transition` (True only
on the transition), `list_for_user` leaves DISCONNECTED out unless asked, `reconnect_needed` answers NEEDS_RECONNECT and
the ACTIVE connections whose credential is gone (`credential_gone`). `connection(...)` builds a stored connection for a
test (technical ids only)."""
from datetime import datetime, timezone
from typing import List, Optional, Set, Tuple

from src.domain.drive_connection import DriveConnection
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_handle import DriveHandle
from src.domain.user_drive import DriveNotConnectedError

from .drive_connection_repo import FakeDriveConnectionRepository

T_CREATED = datetime(2026, 10, 1, tzinfo=timezone.utc)


def connection(user_id: str = "usr-1", connection_id: str = "conn-1", *, ref_tag: str = "a1",
               drive_identity: str = "fake_a:drive-1", kind: str = "fake_a", indexing: bool = True,
               transcribe: bool = False, credential_id: str = "cred-1", account_id: str = "acc-1",
               created_at: datetime = T_CREATED) -> DriveConnection:
    return DriveConnection(connection_id=connection_id, user_id=user_id, account_id=account_id,
                           credential_id=credential_id, kind=kind, drive_identity=drive_identity, ref_tag=ref_tag,
                           indexing_enabled=indexing, transcribe_enabled=transcribe, created_at=created_at)


class FakeDriveConnectionService:

    def __init__(self, repo: Optional[FakeDriveConnectionRepository] = None) -> None:
        self.repo = repo or FakeDriveConnectionRepository()
        self.credential_gone: Set[Tuple[str, str]] = set()   # (user_id, connection_id) whose credential is gone
        self.reads = 0

    async def add(self, conn: DriveConnection) -> DriveConnection:
        await self.repo.create(conn)
        return conn

    async def set(self, user_id: str, connection_id: str, **changes: bool) -> None:
        await self.repo.update(user_id, connection_id, **changes)

    async def get(self, user_id: str, connection_id: str) -> Optional[DriveConnection]:
        self.reads += 1
        return await self.repo.get(user_id, connection_id)

    async def list_for_user(self, user_id: str, *, include_disconnected: bool = False) -> List[DriveConnection]:
        found = await self.repo.list_for_user(user_id)
        return [c for c in found if include_disconnected or c.state is not ConnectionState.DISCONNECTED]

    async def handle_for(self, user_id: str, connection_id: str) -> DriveHandle:
        conn = await self.repo.get(user_id, connection_id)
        if conn is None or conn.state is ConnectionState.DISCONNECTED:
            raise DriveNotConnectedError(connection_id)
        return conn.handle

    async def flag_reconnect(self, handle: DriveHandle) -> bool:
        return await self.repo.transition(handle.user_id, handle.connection_id, ConnectionState.NEEDS_RECONNECT,
                                          datetime.now(timezone.utc))

    async def disconnect(self, user_id: str, connection_id: str) -> bool:
        return await self.repo.transition(user_id, connection_id, ConnectionState.DISCONNECTED,
                                          datetime.now(timezone.utc))

    async def reconnect_needed(self, user_id: str) -> List[DriveConnection]:
        return [c for c in await self.list_for_user(user_id)
                if c.state is ConnectionState.NEEDS_RECONNECT
                or (c.state is ConnectionState.ACTIVE and (user_id, c.connection_id) in self.credential_gone)]

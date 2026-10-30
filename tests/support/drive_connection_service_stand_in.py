"""A test-only stand-in for the READ side of DriveConnectionService (MULTI_DRIVE_RFC §4.4, §9), over the in-memory
`FakeDriveConnectionRepository`.

TRANSITIONAL (slice 1b, part D): the frozen core declares DriveConnectionService with NotImplementedError bodies until
part A fills them; the services of part D call only the read methods below, so their tests run on this subclass, with
the rules the interface documents. Delete it, and point its users at the real service over the same fake repository,
once part A's service lands.

`seed(repo, user_id, ...)` stores a connection and (by default) makes it the user's primary.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional, Union

from src.domain import drive_ref
from src.domain.drive_connection import DriveConnection
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_connection_status import DriveConnectionStatus
from src.domain.drive_handle import DriveHandle
from src.domain.drive_ref import DriveRef
from src.domain.drive_ref_refusal import DriveRefRefusal, disconnected_ref_line, stale_ref_line
from src.domain.drive_resolved_ref import ResolvedDriveRef
from src.domain.drive_user_settings import DriveUserSettings
from src.domain.user_drive import DriveNotConnectedError
from src.services.drive_connection_service import DriveConnectionService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
_LIVE = (ConnectionState.ACTIVE, ConnectionState.NEEDS_RECONNECT)


class StandInDriveConnectionService(DriveConnectionService):

    def __init__(self, repo: FakeDriveConnectionRepository) -> None:
        super().__init__(repo, credentials=None)  # type: ignore[arg-type]   # the read side reads no credential
        self.repo = repo

    async def get(self, user_id: str, connection_id: str) -> Optional[DriveConnection]:
        return await self.repo.get(user_id, connection_id)

    async def list_for_user(self, user_id: str, *, include_disconnected: bool = False) -> List[DriveConnection]:
        found = await self.repo.list_for_user(user_id)
        return [c for c in found if include_disconnected or c.state in _LIVE]

    async def handle_for(self, user_id: str, connection_id: str) -> DriveHandle:
        found = await self.repo.get(user_id, connection_id)
        if found is None or found.state not in _LIVE:
            raise DriveNotConnectedError("no such live connection")
        return found.handle

    async def primary(self, user_id: str) -> Optional[DriveConnection]:
        settings = await self.repo.get_settings(user_id)
        if settings is None or not settings.primary_connection_id:
            return None
        found = await self.repo.get(user_id, settings.primary_connection_id)
        return found if found is not None and found.state is ConnectionState.ACTIVE else None

    async def primary_handle(self, user_id: str) -> DriveHandle:
        found = await self.primary(user_id)
        if found is None:
            raise DriveNotConnectedError("no primary drive connection")
        return found.handle

    async def status(self, user_id: str) -> DriveConnectionStatus:
        settings = await self.repo.get_settings(user_id)
        pointed = (await self.repo.get(user_id, settings.primary_connection_id)
                   if settings is not None and settings.primary_connection_id else None)
        return DriveConnectionStatus.of(pointed)

    async def resolve_ref(self, user_id: str, text: str) -> Optional[Union[ResolvedDriveRef, DriveRefRefusal]]:
        parsed = drive_ref.parse(text)
        if parsed is None:
            return None
        if not isinstance(parsed, DriveRef):
            return DriveRefRefusal(stale_ref_line(str(parsed)))
        mine = [c for c in await self.repo.list_for_user(user_id) if c.ref_tag == parsed.tag]
        if not mine:
            return DriveRefRefusal(stale_ref_line(str(parsed)))
        if mine[0].state is ConnectionState.DISCONNECTED:
            return DriveRefRefusal(disconnected_ref_line(str(parsed)))
        return ResolvedDriveRef(connection=mine[0], item_id=parsed.item_id)


async def seed(repo: FakeDriveConnectionRepository, user_id: str = "usr-1", connection_id: str = "conn-1", *,
               tag: str = "a1", kind: str = "fake_a", indexing_enabled: bool = True,
               state: ConnectionState = ConnectionState.ACTIVE, primary: bool = True,
               drive_identity: str = "", created_at: datetime = _T0) -> DriveConnection:
    """Store one connection (and the primary pointer unless `primary=False`); returns it as stored."""
    connection = DriveConnection(
        connection_id=connection_id, user_id=user_id, account_id="acc-1", credential_id=f"cred-{connection_id}",
        kind=kind, drive_identity=drive_identity or f"{kind}:{connection_id}", ref_tag=tag,
        indexing_enabled=indexing_enabled, state=state, created_at=created_at,
        disconnected_at=created_at if state is ConnectionState.DISCONNECTED else None)
    await repo.create(connection)
    if primary:
        await repo.save_settings(DriveUserSettings(user_id=user_id, primary_connection_id=connection_id))
    return connection

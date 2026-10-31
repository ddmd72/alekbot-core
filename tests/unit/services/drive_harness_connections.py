"""The real `DriveConnectionService` over the single-drive harnesses' connection (MULTI_DRIVE_RFC §4.4, §9).

`LegacyConnections` (drive_index_fakes) derives user `u`'s one connection (id `u`, tag `a1`) live from the harness's
profile flags and scripted drive, which is what the tick reads. A search, an open or a ref resolution must read the SAME
connection, through the production service's rules (primary, resolve_ref, handle_for, status). `HarnessConnectionRepository`
is that connection as a `DriveConnectionRepository`, read-only and read live (a test that flips a profile flag or the
drive's state mid-test is seen by the next read); the user's primary is that connection. Writes are not part of a
harness: they raise.

    connections = harness_connections(w.users, w.drive)
    DriveSearchService(w.repo, w.emb, w.drive, connections, embedding_space=SPACE, ...)
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional, Sequence

from src.domain.drive_connection import DriveConnection
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_user_settings import DriveUserSettings
from src.ports.drive_connection_repository import DriveConnectionRepository
from src.services.drive_connection_service import DriveConnectionService

from .drive_index_fakes import LegacyConnections

HARNESS_TAG = "a1"


class HarnessConnectionRepository(DriveConnectionRepository):

    def __init__(self, users: Any, drive: Any) -> None:
        self._legacy = LegacyConnections(users, drive)

    async def get(self, user_id: str, connection_id: str) -> Optional[DriveConnection]:
        return await self._legacy.get(user_id, connection_id)

    async def list_for_user(self, user_id: str) -> List[DriveConnection]:
        return await self._legacy.list_for_user(user_id)

    async def list_by_state(self, states: Sequence[ConnectionState], *,
                            indexing_enabled: Optional[bool] = None) -> List[DriveConnection]:
        raise NotImplementedError("the harness lists no connections across users")

    async def get_settings(self, user_id: str) -> Optional[DriveUserSettings]:
        found = await self._legacy.get(user_id, user_id)
        return None if found is None else DriveUserSettings(user_id=user_id, primary_connection_id=user_id)

    async def create(self, connection: DriveConnection) -> bool:
        raise NotImplementedError("the harness connection is derived, not stored")

    async def update(self, user_id: str, connection_id: str, **changes: Any) -> Optional[DriveConnection]:
        raise NotImplementedError("the harness connection is derived, not stored")

    async def transition(self, user_id: str, connection_id: str, to: ConnectionState, now: datetime) -> bool:
        raise NotImplementedError("the harness connection is derived, not stored")

    async def save_settings(self, settings: DriveUserSettings) -> None:
        raise NotImplementedError("the harness connection is derived, not stored")


def harness_connections(users: Any, drive: Any, credentials: Any = None) -> DriveConnectionService:
    """The production `DriveConnectionService` reading the harness's one connection per user."""
    return DriveConnectionService(HarnessConnectionRepository(users, drive), credentials)

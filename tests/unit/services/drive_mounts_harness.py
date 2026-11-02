"""Two drives for the mount tests of the search service (multi-drive 2a): `Drive` (connection `u`, tag `a1`, the
primary, the index of the Search v2 fixture) and, on request, `Acme` (connection `conn-2`, tag `b2`, no index yet).
The real `DriveConnectionService` over the in-memory repository; the search service is the fixture's, rebuilt over it."""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from src.domain.drive_connection import DriveConnection
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_mount_name_change import MountNameChange
from src.domain.drive_index import KEYS_VERSION, MIGRATION_GATE_KEY, DriveSyncState
from src.domain.drive_search import DriveSearchRequest
from src.domain.drive_user_settings import DriveUserSettings
from src.services.drive_search_service import DriveSearchService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connections import connection_service

from .drive_search_v2_fixture import AS_OF, SPACE, V2World

CREATED = datetime(2026, 1, 1, tzinfo=timezone.utc)


def drive_connection(connection_id: str, tag: str, name: str, *, drive_identity: str, created_at: datetime,
                     history: Tuple[MountNameChange, ...] = (), state: ConnectionState = ConnectionState.ACTIVE,
                     indexing: bool = True) -> DriveConnection:
    return DriveConnection(
        connection_id=connection_id, user_id="u", account_id="acc-1", credential_id=f"cred-{connection_id}",
        kind="fake_a", drive_identity=drive_identity, ref_tag=tag, mount_name=name, indexing_enabled=indexing,
        state=state, created_at=created_at, disconnected_at=created_at if state is ConnectionState.DISCONNECTED else None,
        name_history=history or (MountNameChange(name, created_at),))


class MountedWorld(V2World):
    """The v2 fixture's index served through a connection service holding `Drive` and, when `acme`, `Acme`."""

    def __init__(self, *, acme: bool = True, acme_history: Optional[Tuple[MountNameChange, ...]] = None,
                 acme_name: str = "Acme") -> None:
        super().__init__()
        self.conns = FakeDriveConnectionRepository()
        self.primary = drive_connection("u", "a1", "Drive", drive_identity="fake:d1", created_at=CREATED)
        self.acme = drive_connection("conn-2", "b2", acme_name, drive_identity="fake:d2",
                                     created_at=CREATED + timedelta(days=1), history=acme_history or ())
        self._acme_on = acme
        self.connections = connection_service(self.conns)
        counter = itertools.count(1)
        self.service = DriveSearchService(self.repo, self.emb, self.drive, self.connections, embedding_space=SPACE,
                                          clock=lambda: self.now, nonce=lambda: f"nonce{next(counter):04d}")

    async def open(self) -> "MountedWorld":
        await self.conns.create(self.primary)
        await self.conns.save_settings(DriveUserSettings(user_id="u", primary_connection_id="u"))
        if self._acme_on:
            await self.conns.create(self.acme)
            self.repo.states["conn-2"] = DriveSyncState(
                user_id="u", connection_id="conn-2", drive_identity="fake:d2", root_item_id="root2",
                applied_space=SPACE, applied_versions={MIGRATION_GATE_KEY: KEYS_VERSION}, observed_as_of=AS_OF,
                initial_enumeration_done=True)   # an index with no records: a search of it answers ok, tagged b2
        return self

    async def find(self, query: str = "", *, drives: str = "", **kw):
        return await self.service.search("u", DriveSearchRequest(query=query, **kw), drives=drives)


def renamed_acme(former: str = "Acme Hamburg", current: str = "Acme", days_ago: int = 3) -> Tuple[MountNameChange, ...]:
    """A history in which the connection took `former` at creation and `current` `days_ago` days before the fixture's now."""
    return (MountNameChange(former, CREATED + timedelta(days=1)),
            MountNameChange(current, AS_OF - timedelta(days=days_ago)))

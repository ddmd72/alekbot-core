"""FakeDriveConnectionRepository — the in-memory twin of `DriveConnectionRepository` (MULTI_DRIVE_RFC §3, §4.4).

A fake with semantics, not a mock: it applies the port's rules (create never overwrites, `update` never touches the
state, `transition` follows `can_transition` atomically under a lock, the orders the port promises) so a service test
asserts what ends up stored. `connections` and `settings` are the stored documents, keyed by `(user_id,
connection_id)` and `user_id`, for assertions.
"""
import asyncio
from dataclasses import replace
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from src.domain.drive_connection import DriveConnection
from src.domain.drive_connection_state import ConnectionState, can_transition
from src.domain.drive_user_settings import DriveUserSettings
from src.domain.provider_account import ProviderAccount
from src.ports.drive_connection_repository import DriveConnectionRepository


class FakeDriveConnectionRepository(DriveConnectionRepository):

    def __init__(self) -> None:
        self.connections: Dict[Tuple[str, str], DriveConnection] = {}
        self.settings: Dict[str, DriveUserSettings] = {}
        self._lock = asyncio.Lock()

    async def create(self, connection: DriveConnection) -> bool:
        async with self._lock:
            key = (connection.user_id, connection.connection_id)
            if key in self.connections:
                return False
            self.connections[key] = connection
            return True

    async def get(self, user_id: str, connection_id: str) -> Optional[DriveConnection]:
        return self.connections.get((user_id, connection_id))

    async def list_for_user(self, user_id: str) -> List[DriveConnection]:
        return sorted((c for (u, _), c in self.connections.items() if u == user_id),
                      key=lambda c: (c.created_at, c.connection_id))

    async def list_by_state(self, states: Sequence[ConnectionState], *,
                            indexing_enabled: Optional[bool] = None) -> List[DriveConnection]:
        wanted = set(states)
        found = [c for c in self.connections.values() if c.state in wanted
                 and (indexing_enabled is None or c.indexing_enabled is indexing_enabled)]
        return sorted(found, key=lambda c: (c.user_id, c.created_at, c.connection_id))

    async def update(self, user_id: str, connection_id: str, *, credential_id: Optional[str] = None,
                     drive_identity: Optional[str] = None, mount_name: Optional[str] = None,
                     indexing_enabled: Optional[bool] = None,
                     transcribe_enabled: Optional[bool] = None,
                     provider_account: Optional[ProviderAccount] = None) -> Optional[DriveConnection]:
        given = {"credential_id": credential_id, "drive_identity": drive_identity, "mount_name": mount_name,
                 "indexing_enabled": indexing_enabled, "transcribe_enabled": transcribe_enabled,
                 "provider_account": provider_account}
        async with self._lock:
            current = self.connections.get((user_id, connection_id))
            if current is None:
                return None
            updated = replace(current, **{k: v for k, v in given.items() if v is not None})
            self.connections[(user_id, connection_id)] = updated
            return updated

    async def transition(self, user_id: str, connection_id: str, to: ConnectionState, now: datetime) -> bool:
        async with self._lock:
            current = self.connections.get((user_id, connection_id))
            if current is None or not can_transition(current.state, to):
                return False
            self.connections[(user_id, connection_id)] = current.with_state(to, now)
            return True

    async def get_settings(self, user_id: str) -> Optional[DriveUserSettings]:
        return self.settings.get(user_id)

    async def save_settings(self, settings: DriveUserSettings) -> None:
        self.settings[settings.user_id] = settings

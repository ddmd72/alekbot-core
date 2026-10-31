"""DriveConnectionRepository — drive connections and the user's drive settings (MULTI_DRIVE_RFC §3, §4.4).

Connections live at `{prefix}drive_connections/{user_id}:{connection_id}` (`DriveConnection.doc_id`), settings at
`{prefix}drive_user_settings/{user_id}`. Tenancy is structural: every read is addressed by the user, and a stored
document whose `user_id` differs from its address is treated as absent (and logged at ERROR by the adapter).
Connections are never deleted: a DISCONNECTED document keeps its tag reserved forever (§4.4).

Writes:
- `create` refuses (False, nothing written) when the document exists: a connection id is never reused.
- `update` changes only the fields given (None keeps a field); it never touches `state` or `disconnected_at`, so a
  settings write cannot lose a concurrent state transition.
- `transition` moves the state atomically under `can_transition` (domain), stamping `disconnected_at` on
  DISCONNECTED; True only when the state changed (the False->True edge the reconnect notice needs).
"""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional, Sequence

from src.domain.drive_connection import DriveConnection
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_user_settings import DriveUserSettings
from src.domain.provider_account import ProviderAccount


class DriveConnectionRepository(ABC):

    @abstractmethod
    async def create(self, connection: DriveConnection) -> bool:
        """Store a new connection; False when a document with its id exists (nothing written)."""

    @abstractmethod
    async def get(self, user_id: str, connection_id: str) -> Optional[DriveConnection]:
        """One connection of the user, in any state; None when absent."""

    @abstractmethod
    async def list_for_user(self, user_id: str) -> List[DriveConnection]:
        """Every connection of the user, every state included, oldest `created_at` first."""

    @abstractmethod
    async def list_by_state(self, states: Sequence[ConnectionState], *,
                            indexing_enabled: Optional[bool] = None) -> List[DriveConnection]:
        """Connections of every user in one of `states` (the fan-out, the reconnect sweep), optionally only those whose
        `indexing_enabled` equals the value given. Ordered by `(user_id, created_at)`."""

    @abstractmethod
    async def update(self, user_id: str, connection_id: str, *, credential_id: Optional[str] = None,
                     drive_identity: Optional[str] = None, mount_name: Optional[str] = None,
                     indexing_enabled: Optional[bool] = None,
                     transcribe_enabled: Optional[bool] = None,
                     provider_account: Optional[ProviderAccount] = None) -> Optional[DriveConnection]:
        """Set the fields given (None keeps a field) and return the stored connection after the write; None when the
        connection does not exist (nothing written). Never changes `state` or `disconnected_at`."""

    @abstractmethod
    async def transition(self, user_id: str, connection_id: str, to: ConnectionState, now: datetime) -> bool:
        """Atomically move the connection to `to` when `can_transition(current, to)` holds; True only when the state
        changed. False for an absent connection, a move to the state already held, or any move out of DISCONNECTED."""

    @abstractmethod
    async def get_settings(self, user_id: str) -> Optional[DriveUserSettings]:
        """The user's drive settings; None before the first write."""

    @abstractmethod
    async def save_settings(self, settings: DriveUserSettings) -> None:
        """Write the user's drive settings whole."""

"""A drive connection: one mounted drive of one user (MULTI_DRIVE_RFC §4.1, §4.4, E1).

The credential is the grant (tokens, provider account); the connection is the mount a grant reads. Stored at
`{prefix}drive_connections/{user_id}:{connection_id}` (structural tenancy, as credentials).

Fields (stage 1; stage 2 adds the scope, write folder and searched-by-default fields):
- `connection_id`: random, immutable, globally unique; no `:` or `/` (it prefixes every connection-scoped key).
- `user_id`, `account_id`: the tenant.
- `credential_id`: the grant the adapter reads (`DriveHandle.credential_id`); a reconnect may point it at a new grant.
- `kind`: the connector kind (the router dispatches on it); a value from the registry, never a literal elsewhere.
- `drive_identity`: the adapter-built `{id_space}:{…}` identity of the drive (§7.2); the mirror roots and ledger key
  on it.
- `ref_tag`: the tag of every `drive:<tag>:<item_id>` ref of this connection (§9); immutable, never reused.
- `mount_name`: the user's name for the drive (D17); `DEFAULT_MOUNT_NAME` for the first one (E11).
- `indexing_enabled`, `transcribe_enabled`: the per-connection switches that were per user before.
- `state`, `created_at`, `disconnected_at`: `disconnected_at` is set exactly when the state is DISCONNECTED.

Drives read reconnect state only from here (`state`), never from the credential.

Public interface:
    DriveConnection (frozen; change it with `dataclasses.replace` or `with_state`)
    DriveConnection.handle -> DriveHandle;  .scope -> ConnectionScope;  .doc_id -> str
    DriveConnection.with_state(to, now) -> DriveConnection     ValueError when the transition is not allowed
    connection_doc_id(scope) -> str
    DEFAULT_MOUNT_NAME
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Optional

from .connection_scope import ConnectionScope, check_connection_id
from .drive_connection_state import ConnectionState, can_transition
from .drive_handle import DriveHandle
from .drive_ref import is_valid_tag

DEFAULT_MOUNT_NAME = "Drive"


@dataclass(frozen=True, kw_only=True)
class DriveConnection:
    connection_id: str
    user_id: str
    account_id: str
    credential_id: str
    kind: str
    drive_identity: str
    ref_tag: str
    mount_name: str = DEFAULT_MOUNT_NAME
    indexing_enabled: bool = False
    transcribe_enabled: bool = False
    state: ConnectionState = ConnectionState.ACTIVE
    created_at: datetime
    disconnected_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        check_connection_id(self.connection_id)
        for name in ("user_id", "account_id", "credential_id", "kind", "drive_identity", "mount_name"):
            if not getattr(self, name):
                raise ValueError(f"DriveConnection needs a {name}")
        if not is_valid_tag(self.ref_tag):
            raise ValueError(f"not a drive ref tag: {self.ref_tag!r}")
        if (self.state is ConnectionState.DISCONNECTED) != (self.disconnected_at is not None):
            raise ValueError("disconnected_at is set exactly when the state is DISCONNECTED")

    @property
    def handle(self) -> DriveHandle:
        return DriveHandle(user_id=self.user_id, connection_id=self.connection_id,
                           credential_id=self.credential_id, kind=self.kind)

    @property
    def scope(self) -> ConnectionScope:
        return ConnectionScope(user_id=self.user_id, connection_id=self.connection_id)

    @property
    def doc_id(self) -> str:
        return connection_doc_id(self.scope)

    def with_state(self, to: ConnectionState, now: datetime) -> DriveConnection:
        """The connection moved to `to` at `now` (DISCONNECTED stamps `disconnected_at`). ValueError when
        `can_transition(state, to)` refuses, a move to the state already held included."""
        if not can_transition(self.state, to):
            raise ValueError(f"no transition {self.state.value} -> {to.value}")
        return replace(self, state=to, disconnected_at=now if to is ConnectionState.DISCONNECTED else None)


def connection_doc_id(scope: ConnectionScope) -> str:
    """`{user_id}:{connection_id}`: the connection document's id."""
    return f"{scope.user_id}:{scope.connection_id}"

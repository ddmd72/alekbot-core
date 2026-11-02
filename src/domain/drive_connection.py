"""A drive connection: one mounted drive of one user (MULTI_DRIVE_RFC §4.1, §4.4, E1).

The credential is the grant (tokens, provider account); the connection is the mount a grant reads. Stored at
`{prefix}drive_connections/{user_id}:{connection_id}` (structural tenancy, as credentials).

Fields:
- `connection_id`: random, immutable, globally unique; no `:` or `/` (it prefixes every connection-scoped key).
- `user_id`, `account_id`: the tenant.
- `credential_id`: the grant the adapter reads (`DriveHandle.credential_id`); a reconnect may point it at a new grant.
- `kind`: the connector kind (the router dispatches on it); a value from the registry, never a literal elsewhere.
- `drive_identity`: the adapter-built `{id_space}:{…}` identity of the drive (§7.2); the mirror roots and ledger key
  on it.
- `ref_tag`: the tag of every `drive:<tag>:<item_id>` ref of this connection (§9); immutable, never reused.
- `mount_name`: the user's name for the drive (D17); `DEFAULT_MOUNT_NAME` for the first one (E11).
- `name_history`: every name the connection took, oldest first, each with the instant it was taken (§11.8); the last
  entry is `mount_name` (compared by `mount_name_key`). Empty on a connection stored before the field (no former names).
- `indexing_enabled`, `transcribe_enabled`: the per-connection switches that were per user before.
- `provider_account`: the provider account (realm, subject; label empty) of the grant the connection was activated with,
  the key of its initial build, so a tick reads it here and never decrypts the credential; None on a connection stored
  before the field (the fan-out backfills it).
- `state`, `created_at`, `disconnected_at`: `disconnected_at` is set exactly when the state is DISCONNECTED.
- The Cabinet settings (stage 2, §11.3, D4, D11; written only by `DriveConnectionService`): `searched_by_default` (an
  empty `drives` value searches it); `write_folder_id` (None: read-only; `ROOT_FOLDER`: the drive's root, the migrated
  connection's and the dataclass default; else a folder id; a new connection starts None); `scope_folder_ids` and
  `scope_whole_drive` (never both; neither: the feed's default scope).

Drives read reconnect state only from here (`state`), never from the credential.

Public interface:
    DriveConnection (frozen; change it with `dataclasses.replace` or `with_state`)
    DriveConnection.handle -> DriveHandle;  .scope -> ConnectionScope;  .doc_id -> str;  .scope_key -> str
    DriveConnection.with_state(to, now) -> DriveConnection     ValueError when the transition is not allowed
    connection_doc_id(scope) -> str
    DEFAULT_MOUNT_NAME, ROOT_FOLDER
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Optional, Tuple

from .connection_scope import ConnectionScope, check_connection_id
from .drive_connection_state import ConnectionState, can_transition
from .drive_handle import DriveHandle
from .drive_mount_name_change import MountNameChange, mount_name_key
from .drive_ref import is_valid_tag
from .provider_account import ProviderAccount

DEFAULT_MOUNT_NAME = "Drive"
ROOT_FOLDER = ""        # "the drive's root" as a write folder


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
    provider_account: Optional[ProviderAccount] = None
    name_history: Tuple[MountNameChange, ...] = ()
    searched_by_default: bool = True
    write_folder_id: Optional[str] = ROOT_FOLDER
    scope_folder_ids: Tuple[str, ...] = ()
    scope_whole_drive: bool = False

    def __post_init__(self) -> None:
        check_connection_id(self.connection_id)
        for name in ("user_id", "account_id", "credential_id", "kind", "drive_identity", "mount_name"):
            if not getattr(self, name):
                raise ValueError(f"DriveConnection needs a {name}")
        if not is_valid_tag(self.ref_tag):
            raise ValueError(f"not a drive ref tag: {self.ref_tag!r}")
        if (self.state is ConnectionState.DISCONNECTED) != (self.disconnected_at is not None):
            raise ValueError("disconnected_at is set exactly when the state is DISCONNECTED")
        if self.name_history and mount_name_key(self.name_history[-1].name) != mount_name_key(self.mount_name):
            raise ValueError("the last name_history entry must be the current mount_name")
        if self.scope_whole_drive and self.scope_folder_ids:
            raise ValueError("a drive connection's scope is folders or the whole drive, not both")
        if not all(isinstance(f, str) and f for f in self.scope_folder_ids):
            raise ValueError("a scope folder id is a non-empty string")

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

    @property
    def scope_key(self) -> str:
        """`drive_access_policy.scope_key` of this connection's scope."""
        from .drive_access_policy import scope_key   # the policy imports this module (ROOT_FOLDER)
        return scope_key(self.scope_folder_ids, self.scope_whole_drive)

    def with_state(self, to: ConnectionState, now: datetime) -> DriveConnection:
        """The connection moved to `to` at `now` (DISCONNECTED stamps `disconnected_at`). ValueError when
        `can_transition(state, to)` refuses, a move to the state already held included."""
        if not can_transition(self.state, to):
            raise ValueError(f"no transition {self.state.value} -> {to.value}")
        return replace(self, state=to, disconnected_at=now if to is ConnectionState.DISCONNECTED else None)


def connection_doc_id(scope: ConnectionScope) -> str:
    """`{user_id}:{connection_id}`: the connection document's id."""
    return f"{scope.user_id}:{scope.connection_id}"

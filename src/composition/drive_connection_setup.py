"""Wiring of drive connections (MULTI_DRIVE_RFC §4.4, §5.2, §6): the connection store, `DriveConnectionService` and the
drive activation of a completed consent.

The drive activation runs after `OAuthConnectService` saved a drive credential (it is the DRIVE entry of
`oauth_connect_setup.build_connect_activations`). It is provider-neutral: a new drive kind adds no code here.
1. The drive identity of the grant: `get_root(handle).drive_identity` through the router. The handle is the matching
   connection's when the consent's credential already backs one (a reconnect); otherwise a probe handle for the
   credential (a connection id derived from the credential id, never stored), whose adapter caches are dropped with
   `forget` right after. A failed read falls back to the matching connection's stored identity; with none, the
   activation raises `ConnectActivationFailed`: the connect fails and the connect service removes the just-saved
   credential (no credential without its connection).
2. `DriveConnectionService.activate(credential, identity)`: create, or reconnect by account.
3. When the connection indexes, a `drive_sync` task for it under a toggle key, so a reconnect resumes the index
   without waiting for the fan-out (best effort, logged).

Public interface:
    build_drive_connection_repository(db_client, env_config) -> FirestoreDriveConnectionRepository
    build_drive_connection_service(db_client, env_config, *, credentials) -> DriveConnectionService
    build_drive_activation(*, connections, drive, task_queue, clock=None) -> Activation
    probe_connection_id(credential_id) -> str
"""
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from ..adapters.firestore_drive_connection_repo import FirestoreDriveConnectionRepository
from ..config.environment import EnvironmentConfig
from ..domain.drive_handle import DriveHandle
from ..domain.drive_index_params import TICK_DEADLINE_S
from ..domain.drive_task_keys import DRIVE_SYNC_TASK, scope_payload, toggle_task_key
from ..domain.exceptions import ConnectActivationFailed
from ..domain.oauth_credential import OAuthCredential
from ..ports.oauth_credentials_port import OAuthCredentialsPort
from ..ports.task_queue import TaskQueue
from ..ports.user_drive_port import UserDrivePort
from ..services.drive_connection_service import DriveConnectionService
from ..utils.logger import logger

Activation = Callable[[OAuthCredential], Awaitable[None]]

_PROBE_PREFIX = "probe-"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def build_drive_connection_repository(db_client: Any, env_config: EnvironmentConfig) -> FirestoreDriveConnectionRepository:
    return FirestoreDriveConnectionRepository(db_client, env_config.drive_connections_collection,
                                              env_config.drive_user_settings_collection)


def build_drive_connection_service(db_client: Any, env_config: EnvironmentConfig, *,
                                   credentials: OAuthCredentialsPort) -> DriveConnectionService:
    """The one drive connection service of a process, over the Firestore store and the credential store."""
    return DriveConnectionService(build_drive_connection_repository(db_client, env_config), credentials)


def probe_connection_id(credential_id: str) -> str:
    """The connection id of the probe handle that reads a new grant's drive identity: never stored, never a key."""
    return f"{_PROBE_PREFIX}{credential_id}"


def build_drive_activation(*, connections: DriveConnectionService, drive: UserDrivePort,
                           task_queue: Optional[TaskQueue],
                           clock: Optional[Callable[[], datetime]] = None) -> Activation:
    """The DRIVE activation (module docstring). `drive` is the router (`DriveProviderRouter`); `task_queue` the drive
    queue (None: no nudge)."""
    now = clock or _utcnow

    async def read_identity(handle: DriveHandle) -> str:
        try:
            return (await drive.get_root(handle)).drive_identity
        except Exception as exc:
            logger.warning(f"⚠️ Drive identity not read at connect: user={handle.user_id[:8]} "
                           f"kind={handle.kind}: {type(exc).__name__}")
            return ""

    async def forget(handle: DriveHandle) -> None:
        try:
            await drive.forget(handle)
        except Exception as exc:
            logger.warning(f"⚠️ Dropping the probe's caches failed: user={handle.user_id[:8]}: {type(exc).__name__}")

    async def activate(credential: OAuthCredential) -> None:
        user_id = credential.user_id
        existing = next((c for c in await connections.list_for_user(user_id)
                         if c.kind == credential.kind and c.credential_id == credential.credential_id), None)
        if existing is not None:
            identity = await read_identity(existing.handle) or existing.drive_identity
        else:
            probe = DriveHandle(user_id=user_id, connection_id=probe_connection_id(credential.credential_id),
                                credential_id=credential.credential_id, kind=credential.kind)
            identity = await read_identity(probe)
            await forget(probe)
        if not identity:
            logger.error(f"💥 Drive connect of user={user_id[:8]} kind={credential.kind}: the drive identity could not "
                         f"be read; no connection created, the connect fails")
            raise ConnectActivationFailed("the drive identity of a new grant could not be read")
        connection = await connections.activate(credential, identity)
        if not connection.indexing_enabled or task_queue is None:
            return
        try:
            await task_queue.enqueue_worker_task(DRIVE_SYNC_TASK, scope_payload(connection.scope),
                                                 deadline_seconds=TICK_DEADLINE_S,
                                                 dedup_key=toggle_task_key(connection.scope, now()))
        except Exception as exc:     # the fan-out reaches the connection within TICK_INTERVAL
            logger.error(f"💥 Enqueueing the drive sync after a connect failed for user={user_id[:8]}: "
                         f"{type(exc).__name__}", exc_info=True)

    return activate

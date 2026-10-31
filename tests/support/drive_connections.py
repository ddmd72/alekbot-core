"""The real `DriveConnectionService` over the in-memory `FakeDriveConnectionRepository`, for service tests that need a
user's drive connections (MULTI_DRIVE_RFC §4.4, §9): technical ids only.

    repo = FakeDriveConnectionRepository()
    conn = await seed(repo, "usr-1", "conn-1", tag="a1")        # ACTIVE, and the user's primary
    connections = connection_service(repo)

`seed` stores one connection (and, unless `primary=False`, points the user's primary at it); `connection_service`
builds the production service. Its credential port is only read by `flag_reconnect` (best effort) and
`reconnect_needed`; pass one when the test reaches those.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from src.domain.drive_connection import DriveConnection
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_user_settings import DriveUserSettings
from src.services.drive_connection_service import DriveConnectionService

from .drive_connection_repo import FakeDriveConnectionRepository

T_SEEDED = datetime(2026, 1, 1, tzinfo=timezone.utc)


async def seed(repo: FakeDriveConnectionRepository, user_id: str = "usr-1", connection_id: str = "conn-1", *,
               tag: str = "a1", kind: str = "fake_a", indexing_enabled: bool = True, transcribe_enabled: bool = False,
               state: ConnectionState = ConnectionState.ACTIVE, primary: bool = True, drive_identity: str = "",
               credential_id: str = "", account_id: str = "acc-1",
               created_at: datetime = T_SEEDED) -> DriveConnection:
    """Store one connection (and the primary pointer unless `primary=False`); returns it as stored. The credential id
    defaults to `cred-{connection_id}`, the drive identity to `{kind}:{connection_id}`."""
    connection = DriveConnection(
        connection_id=connection_id, user_id=user_id, account_id=account_id,
        credential_id=credential_id or f"cred-{connection_id}", kind=kind,
        drive_identity=drive_identity or f"{kind}:{connection_id}", ref_tag=tag,
        indexing_enabled=indexing_enabled, transcribe_enabled=transcribe_enabled, state=state, created_at=created_at,
        disconnected_at=created_at if state is ConnectionState.DISCONNECTED else None)
    await repo.create(connection)
    if primary:
        await repo.save_settings(DriveUserSettings(user_id=user_id, primary_connection_id=connection_id))
    return connection


def connection_service(repo: Optional[FakeDriveConnectionRepository] = None,
                       credentials: Any = None) -> DriveConnectionService:
    """The production `DriveConnectionService` over `repo` (a new empty one by default)."""
    return DriveConnectionService(repo if repo is not None else FakeDriveConnectionRepository(), credentials)

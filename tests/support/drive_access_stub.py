"""Access-policy stand-ins for tests that build a `FileConversionService` or a `UserDriveService` over a mocked drive
(MULTI_DRIVE_RFC §11.3, slice 2b): technical ids only.

- `allow_access()`: a `UserDriveService` stand-in whose `check_access` allows everything (for a conversion service the
  test does not exercise the policy through).
- `policy_connections(drive, handle)`: the production `DriveConnectionService` over a repository holding the handle's
  connection with the defaults (App Folder: whole area readable, the root as the write folder), and `drive.capabilities`
  answering a writable AREA drive, so a `UserDriveService(drive, policy_connections(drive, handle))` reads its access.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock

from src.domain.drive_capabilities import DriveCapabilities
from src.domain.drive_connection import DriveConnection
from src.domain.drive_handle import DriveHandle
from src.services.drive_connection_service import DriveConnectionService
from src.services.user_drive_service import UserDriveService

from .drive_connection_repo import FakeDriveConnectionRepository
from .drive_handles import drive_handle

CAPABILITIES = DriveCapabilities(max_path_chars=400)


def allow_access() -> Any:
    return AsyncMock(spec=UserDriveService)


def policy_connections(drive: Any, handle: Optional[DriveHandle] = None,
                       repo: Optional[FakeDriveConnectionRepository] = None) -> DriveConnectionService:
    handle = handle or drive_handle("u1")
    repo = repo if repo is not None else FakeDriveConnectionRepository()
    repo.connections.setdefault((handle.user_id, handle.connection_id), DriveConnection(
        connection_id=handle.connection_id, user_id=handle.user_id, account_id="acc-1",
        credential_id=handle.credential_id, kind=handle.kind, drive_identity=f"{handle.kind}:{handle.connection_id}",
        ref_tag="a1", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    if hasattr(drive.capabilities, "return_value"):
        drive.capabilities.return_value = CAPABILITIES
    return DriveConnectionService(repo, None)   # type: ignore[arg-type]

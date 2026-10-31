"""A spec mock of `UserDriveService` whose connection reads are the service's own (MULTI_DRIVE_RFC §6, §9): technical
ids only.

Tests of the drive service's callers (DriveOperations, FileManagementAgent) script the drive operations (`save`,
`move`, `get_item`, ...) on an `AsyncMock(spec=UserDriveService)`; since stage 1 every caller first asks the service
which connection a path or a ref is on. `spec_drive_service(*user_ids)` answers `status`, `primary` and `resolve_ref`
with the production `UserDriveService` methods over the production `DriveConnectionService`, each user with one ACTIVE
primary connection `conn-1` tagged `a1` (`tests/support/drive_service_connections.py`); everything else stays a spec
mock to script. `connection_of(user_id)` / `handle_of(user_id)` are that connection and its handle, for asserting
what a drive call or a search lookup was given.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

from src.domain.drive_connection import DriveConnection
from src.domain.drive_handle import DriveHandle
from src.services.user_drive_service import UserDriveService

from .drive_connection_repo import FakeDriveConnectionRepository
from .drive_service_connections import REF_TAG, seed_user, seeded_connections


def spec_drive_service(*user_ids: str, tag: str = REF_TAG) -> AsyncMock:
    """`AsyncMock(spec=UserDriveService)` with the service's real connection reads for `user_ids` (default "u1")."""
    mock = AsyncMock(spec=UserDriveService)
    reads = UserDriveService(drive=None, connections=seeded_connections(*(user_ids or ("u1",)), tag=tag))  # type: ignore[arg-type]
    mock.status.side_effect = reads.status
    mock.primary.side_effect = reads.primary
    mock.resolve_ref.side_effect = reads.resolve_ref
    return mock


def connection_of(user_id: str = "u1", tag: str = REF_TAG) -> DriveConnection:
    """The connection `spec_drive_service` seeds for `user_id` (equal to the one its reads return)."""
    return seed_user(FakeDriveConnectionRepository(), user_id, tag=tag)


def handle_of(user_id: str = "u1", tag: str = REF_TAG) -> DriveHandle:
    """That connection's handle: what a drive call on it is given."""
    return connection_of(user_id, tag).handle

"""`DriveOperations` mounts overview: a failing scope-folder read degrades that mount's line (it shows no "indexes only"),
never the whole `/` listing. Invented cast only."""
import logging
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from src.domain.drive_feed_scope import FeedScope
from src.infrastructure.drive_operations import DriveOperations
from src.ports.file_storage_port import FileStoragePort
from src.services.drive_connection_service import DriveConnectionService
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_access_stub import allow_access
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connections import seed
from tests.support.fake_drive_adapter import FakeDriveAdapter

CTX = {"account_id": "acc-1"}


class _ScopeReadFails(UserDriveService):
    async def scope_folder_paths(self, handle, limit=5):
        raise OSError("scope read failed for Acme Hamburg")


@pytest.fixture
async def ops():
    repo = FakeDriveConnectionRepository()
    drive = FakeDriveAdapter("fake_b", feed_scope=FeedScope.ACCOUNT)
    connections = DriveConnectionService(repo, None)
    conn = await seed(repo, connection_id="conn-2", tag="b2", kind="fake_b")
    hamburg = drive.plant_parentless(conn.handle, "Acme Hamburg", folder=True)
    repo.connections[("usr-1", "conn-2")] = replace(conn, mount_name="Acme", scope_folder_ids=(hamburg,))
    conversion = FileConversionService(storage=AsyncMock(spec=FileStoragePort), drive=drive, connections=connections,
                                       access=allow_access())
    return DriveOperations(conversion_service=conversion, drive_service=_ScopeReadFails(drive, connections))


async def test_a_failing_scope_read_leaves_the_mount_listed_without_its_scope(ops, caplog):
    with caplog.at_level(logging.WARNING):
        out = await ops.list_folder(CTX, "usr-1", "/")
    assert "- Acme:" in out.text and "indexes only" not in out.text
    assert "scope folders read failed" in caplog.text

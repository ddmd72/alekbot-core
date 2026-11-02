"""G11 through `DriveOperations`: a drive whose root is outside its scope lists its chosen folders under
`Folders indexed on <mount>:`, and the `/` listing names them in `indexes only`. Invented cast only."""
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


@pytest.fixture
async def world():
    class World:
        pass
    w = World()
    w.repo = FakeDriveConnectionRepository()
    w.drive = FakeDriveAdapter("fake_b", feed_scope=FeedScope.ACCOUNT)
    w.connections = DriveConnectionService(w.repo, None)
    conn = await seed(w.repo, connection_id="conn-2", tag="b2", kind="fake_b")
    w.hamburg = w.drive.plant_parentless(conn.handle, "Acme Hamburg", folder=True)
    w.q3 = w.drive.plant(conn.handle, w.hamburg, "Q3", folder=True)
    w.drive.plant(conn.handle, w.q3, "Q3 budget", b"lukas weber's share")
    w.repo.connections[("usr-1", "conn-2")] = replace(conn, mount_name="Acme", scope_folder_ids=(w.q3,))
    conversion = FileConversionService(storage=AsyncMock(spec=FileStoragePort), drive=w.drive,
                                       connections=w.connections, access=allow_access())
    w.ops = DriveOperations(conversion_service=conversion, drive_service=UserDriveService(w.drive, w.connections))
    return w


async def test_the_mount_listing_heads_the_chosen_folders_with_their_refs(world):
    out = await world.ops.list_folder(CTX, "usr-1", "Acme")
    lines = out.text.splitlines()
    assert lines[0] == "Folders indexed on Acme:"
    assert lines[1] == f"  [Drive folder: Acme/Acme Hamburg/Q3/ ref=drive:b2:{world.q3}]"
    assert len(lines) == 2 and "(empty)" not in out.text


async def test_the_slash_listing_shows_where_the_drive_indexes(world):
    out = await world.ops.list_folder(CTX, "usr-1", "/")
    assert "indexes only Acme/Acme Hamburg/Q3" in out.text


async def test_a_listing_inside_the_chosen_folder_keeps_its_own_heading(world):
    out = await world.ops.list_folder(CTX, "usr-1", "Acme/Acme Hamburg/Q3")
    assert out.text.startswith("[Drive folder: Acme/Acme Hamburg/Q3/") and "Folders indexed" not in out.text

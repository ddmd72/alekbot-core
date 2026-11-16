"""`UserDriveService.save_to_primary` and `can_save_to_primary`: the primary connection's write folder, the Inbox by
default, a named folder created inside it. Real service over `FakeDriveAdapter`."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.domain.drive_connection import DriveConnection
from src.domain.drive_user_settings import DriveUserSettings
from src.domain.user_drive import DriveAccessRefused, DriveNotConnectedError
from src.services.drive_connection_service import DriveConnectionService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.fake_drive_adapter import FakeDriveAdapter

NOW = datetime(2026, 11, 16, 9, 30, tzinfo=timezone.utc)
NAME = "2025-08-10 14.05 Check-in at Albergo Immaginario.md"
TYPE = "text/markdown; charset=utf-8"


class World:
    def __init__(self, primary=True, writable=True):
        self.fake = FakeDriveAdapter("fake_b", writable=writable)
        self.repo = FakeDriveConnectionRepository()
        self.conn = DriveConnection(connection_id="conn-2", user_id="usr-1", account_id="acc-1", credential_id="c2",
                                    kind="fake_b", drive_identity="fake_b:d2", ref_tag="b2", mount_name="Acme",
                                    created_at=NOW)
        self.repo.connections[("usr-1", "conn-2")] = self.conn
        if primary:
            self.repo.settings["usr-1"] = DriveUserSettings(user_id="usr-1", primary_connection_id="conn-2")
        self.root = self.fake.tree(self.conn.handle).root.item_id
        self.rd = self.fake.plant(self.conn.handle, self.root, "R&D", folder=True)
        self.drive = UserDriveService(self.fake, DriveConnectionService(self.repo, None))

    def settings(self, **changes):
        self.repo.connections[("usr-1", "conn-2")] = replace(self.conn, **changes)

    def paths(self):
        tree = self.fake.tree(self.conn.handle)
        return {tree.path(n) for n in tree.nodes.values()}


async def test_no_folder_lands_in_the_inbox_of_the_write_folder():
    w = World()
    w.settings(write_folder_id=w.rd)
    saved, path = await w.drive.save_to_primary("usr-1", b"# hi", NAME, TYPE)
    assert path == f"Acme/R&D/Inbox/{NAME}"
    assert saved.item.name == NAME
    assert f"R&D/Inbox/{NAME}" in w.paths()


async def test_a_named_folder_is_created_inside_the_write_folder():
    w = World()
    w.settings(write_folder_id=w.rd)
    _, path = await w.drive.save_to_primary("usr-1", b"x", NAME, TYPE, "Calls")
    assert path == f"Acme/R&D/Calls/{NAME}"
    assert "R&D/Calls" in w.paths()


async def test_the_root_write_folder_puts_the_inbox_at_the_top():
    w = World()
    _, path = await w.drive.save_to_primary("usr-1", b"x", NAME, TYPE)
    assert path == f"Acme/Inbox/{NAME}"


async def test_no_primary_is_not_connected():
    w = World(primary=False)
    with pytest.raises(DriveNotConnectedError):
        await w.drive.save_to_primary("usr-1", b"x", NAME, TYPE)


async def test_no_write_folder_is_refused_and_creates_nothing():
    w = World()
    w.settings(write_folder_id=None)
    before = w.paths()
    with pytest.raises(DriveAccessRefused):
        await w.drive.save_to_primary("usr-1", b"x", NAME, TYPE)
    assert w.paths() == before


async def test_the_predicate_is_true_for_a_writable_primary_with_a_write_folder():
    w = World()
    assert await w.drive.can_save_to_primary("usr-1") is True


async def test_the_predicate_is_false_without_a_primary():
    assert await World(primary=False).drive.can_save_to_primary("usr-1") is False


async def test_the_predicate_is_false_on_a_read_only_drive():
    assert await World(writable=False).drive.can_save_to_primary("usr-1") is False


async def test_the_predicate_is_false_without_a_write_folder():
    w = World()
    w.settings(write_folder_id=None)
    assert await w.drive.can_save_to_primary("usr-1") is False

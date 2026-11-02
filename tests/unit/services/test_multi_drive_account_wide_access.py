"""T-4 stage 2, access half (MULTI_DRIVE_RFC §11.3): `fake_b` in ACCOUNT mode through `UserDriveService`. Nothing is
readable before a scope is chosen; with `R&D` chosen it is readable and `Springfield` is not; writes land only in the
write folder; a parentless item is readable only under the whole drive."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.domain.drive_access_policy import OUTSIDE_SCOPE_LINE, OUTSIDE_WRITE_FOLDER_LINE
from src.domain.drive_connection import DriveConnection
from src.domain.drive_feed_scope import FeedScope
from src.domain.user_drive import DriveAccessRefused
from src.services.drive_connection_service import DriveConnectionService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.fake_drive_adapter import FakeDriveAdapter

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
OUT_SCOPE = OUTSIDE_SCOPE_LINE.format(drive="Acme")


class World:
    def __init__(self, **settings):
        self.fake = FakeDriveAdapter("fake_b", feed_scope=FeedScope.ACCOUNT)
        self.repo = FakeDriveConnectionRepository()
        self.connection = DriveConnection(
            connection_id="conn-2", user_id="usr-1", account_id="acc-1", credential_id="cred-2", kind="fake_b",
            drive_identity="fake_b:d2", ref_tag="b2", mount_name="Acme", created_at=T0, **settings)
        self.repo.connections[("usr-1", "conn-2")] = self.connection
        self.handle = self.connection.handle
        self.service = UserDriveService(self.fake, DriveConnectionService(self.repo, None))
        self.root = self.fake.tree(self.handle).root.item_id
        self.rd = self.fake.plant(self.handle, self.root, "R&D", folder=True)
        self.plan = self.fake.plant(self.handle, self.rd, "Tom's plan.docx", b"plan")
        self.springfield = self.fake.plant(self.handle, self.root, "Springfield", folder=True)
        self.lab = self.fake.plant(self.handle, self.springfield, "lab-report.pdf", b"lab")
        self.shared = self.fake.plant_parentless(self.handle, "birth-certificate.pdf", b"jamie")

    def settings(self, **changes):
        self.repo.connections[("usr-1", "conn-2")] = replace(self.connection, **changes)


async def line_of(coro):
    with pytest.raises(DriveAccessRefused) as caught:
        await coro
    return caught.value.line


async def test_nothing_is_readable_before_a_scope_is_chosen():
    w = World()
    assert await line_of(w.service.get_item(w.handle, w.plan)) == OUT_SCOPE
    assert await line_of(w.service.list_folder(w.handle, "R&D")) == OUT_SCOPE
    assert await w.service.search(w.handle, "plan") == []


async def test_with_a_folder_chosen_it_is_readable_and_the_rest_is_not():
    w = World(scope_folder_ids=("item-2",))
    w.settings(scope_folder_ids=(w.rd,))
    assert (await w.service.get_item(w.handle, w.plan)).item_id == w.plan
    assert (await w.service.list_folder(w.handle, "R&D")).folder.item_id == w.rd
    assert await line_of(w.service.get_item(w.handle, w.lab)) == OUT_SCOPE
    assert await line_of(w.service.list_folder(w.handle, "Springfield")) == OUT_SCOPE
    assert await line_of(w.service.get_item(w.handle, w.shared)) == OUT_SCOPE


async def test_writes_only_inside_the_write_folder():
    w = World()
    w.settings(scope_folder_ids=(w.rd, w.springfield), write_folder_id=w.rd)
    out = await w.service.save(w.handle, b"x", "a.txt", "text/plain", "R&D")
    assert out.item.name == "a.txt"
    line = OUTSIDE_WRITE_FOLDER_LINE.format(drive="Acme")
    assert await line_of(w.service.save(w.handle, b"x", "b.txt", "text/plain", "Springfield")) == line
    assert await line_of(w.service.delete(w.handle, w.lab)) == line


async def test_a_parentless_item_is_readable_only_under_the_whole_drive():
    w = World()
    w.settings(scope_folder_ids=(w.rd,))
    assert await line_of(w.service.get_item(w.handle, w.shared)) == OUT_SCOPE
    w.settings(scope_folder_ids=(), scope_whole_drive=True)
    assert (await w.service.get_item(w.handle, w.shared)).item_id == w.shared


async def test_the_root_write_folder_does_not_admit_a_parentless_item():
    w = World(scope_whole_drive=True)
    line = OUTSIDE_WRITE_FOLDER_LINE.format(drive="Acme")
    assert await line_of(w.service.replace_with(w.handle, w.shared, b"x", "text/plain")) == line
    assert (await w.service.replace_with(w.handle, w.plan, b"x", "text/plain")).mode == "replace"

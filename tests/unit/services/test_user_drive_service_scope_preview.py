"""`UserDriveService.scope_preview` (decisions/drive_scope_add_preview.md): a read-only walk that counts what adding
folders to the indexed scope would bring in, with the indexer's own buckets and descent rules, bounded by files,
folders and a deadline. Over `FakeDriveAdapter`."""
from datetime import datetime, timezone

import pytest

from src.domain.drive_connection import DriveConnection
from src.domain.drive_scope_preview import PreviewBucket
from src.services.drive_connection_service import DriveConnectionService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.fake_drive_adapter import FakeDriveAdapter

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


class World:
    def __init__(self):
        self.fake = FakeDriveAdapter("fake_b")
        repo = FakeDriveConnectionRepository()
        connection = DriveConnection(
            connection_id="conn-2", user_id="usr-1", account_id="acc-1", credential_id="cred-2", kind="fake_b",
            drive_identity="fake_b:d2", ref_tag="b2", mount_name="Acme", created_at=T0)
        repo.connections[("usr-1", "conn-2")] = connection
        self.handle = connection.handle
        self.service = UserDriveService(self.fake, DriveConnectionService(repo, None))
        self.root = self.fake.tree(self.handle).root.item_id

    def folder(self, parent, name):
        return self.fake.plant(self.handle, parent, name, folder=True)

    def file(self, parent, name, data=b"x"):
        return self.fake.plant(self.handle, parent, name, data)


@pytest.fixture
def world():
    return World()


async def test_a_nested_tree_is_counted_by_kind_and_folders(world):
    rd = world.folder(world.root, "R&D")
    world.file(rd, "Tom's plan.docx")
    world.file(rd, "wa_dentist_ru.jpg")
    sub = world.folder(rd, "sub-1")
    world.file(sub, "lease_porto.pdf")
    world.file(sub, "audio-1.m4a")
    world.file(sub, "archive.bin")
    result = await world.service.scope_preview(world.handle, [rd])
    assert result.counts == {PreviewBucket.DOCUMENT: 2, PreviewBucket.IMAGE: 1, PreviewBucket.RECORDING: 1,
                             PreviewBucket.OTHER: 1}
    assert (result.total, result.folders, result.capped) == (5, 2, False)


async def test_dot_and_generated_folders_and_repositories_are_not_read(world):
    rd = world.folder(world.root, "R&D")
    world.file(rd, "Tom's plan.docx")
    hidden = world.folder(rd, ".cache")
    world.file(hidden, "lease_porto.pdf")
    generated = world.folder(rd, "node_modules")
    world.file(generated, "lease_porto.pdf")
    repo = world.folder(rd, "Code")
    world.folder(repo, ".git")
    world.file(repo, "lease_porto.pdf")
    result = await world.service.scope_preview(world.handle, [rd])
    assert result.counts == {PreviewBucket.DOCUMENT: 1}
    assert result.folders == 2          # R&D and Code were read; Code's files are opaque


async def test_the_file_cap_stops_the_walk_and_marks_it_capped(world, monkeypatch):
    monkeypatch.setattr("src.services.user_drive_service.PREVIEW_MAX_FILES", 3)
    rd = world.folder(world.root, "R&D")
    for i in range(5):
        world.file(rd, f"scan_{i:04d}.pdf")
    result = await world.service.scope_preview(world.handle, [rd])
    assert (result.total, result.capped) == (3, True)


async def test_exactly_the_cap_is_not_capped(world, monkeypatch):
    monkeypatch.setattr("src.services.user_drive_service.PREVIEW_MAX_FILES", 3)
    rd = world.folder(world.root, "R&D")
    for i in range(3):
        world.file(rd, f"scan_{i:04d}.pdf")
    result = await world.service.scope_preview(world.handle, [rd])
    assert (result.total, result.capped) == (3, False)


async def test_the_folder_cap_stops_the_walk_and_marks_it_capped(world, monkeypatch):
    monkeypatch.setattr("src.services.user_drive_service.PREVIEW_MAX_FOLDERS", 2)
    rd = world.folder(world.root, "R&D")
    for name in ("A", "B", "C"):
        world.file(world.folder(rd, name), "lease_porto.pdf")
    result = await world.service.scope_preview(world.handle, [rd])
    assert (result.folders, result.capped) == (2, True)
    assert result.total < 3


async def test_the_deadline_stops_the_walk_with_an_injected_clock(world):
    rd = world.folder(world.root, "R&D")
    for name in ("A", "B", "C"):
        world.file(world.folder(rd, name), "lease_porto.pdf")
    ticks = iter(range(0, 1000, 10))      # every clock read moves ten seconds on
    result = await world.service.scope_preview(world.handle, [rd], deadline_s=25.0, clock=lambda: float(next(ticks)))
    assert result.capped is True
    assert result.folders < 4


async def test_a_root_gone_from_the_drive_is_skipped(world):
    rd = world.folder(world.root, "R&D")
    world.file(rd, "Tom's plan.docx")
    result = await world.service.scope_preview(world.handle, ["missing-id", rd])
    assert (result.total, result.folders, result.capped) == (1, 1, False)


async def test_a_walk_writes_nothing(world):
    rd = world.folder(world.root, "R&D")
    world.file(rd, "Tom's plan.docx")
    before = len(world.fake.tree(world.handle).log)
    await world.service.scope_preview(world.handle, [rd])
    assert len(world.fake.tree(world.handle).log) == before

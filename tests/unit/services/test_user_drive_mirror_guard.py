"""The Step 1 guard (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.9.8): chat drive writes are refused under, into, or
over the bot's `_Transcripts` folder."""
from unittest.mock import AsyncMock

import pytest

from src.adapters.in_memory_drive_mirror_repo import InMemoryDriveMirrorRepository
from src.domain.drive_mirror import MirrorRoot
from src.domain.user_drive import DriveItem, DriveItemNotFoundError, DriveMirrorWriteRefused
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.ports.user_drive_port import UserDrivePort
from src.services.drive_connection_service import DriveConnectionService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_handles import drive_handle

H = drive_handle("u1")   # the connection the mutations act on (MULTI_DRIVE_RFC §6)
DRIVE = "fake:d1"        # its drive identity (§7.2): the mirror roots are keyed by it (§10)
ROOT = DriveItem(item_id="area", name="Alek-bot", path="", is_folder=True, drive_identity=DRIVE)
MIRROR = DriveItem(item_id="mr", name="_Transcripts", path="_Transcripts", is_folder=True)
OLD_MIRROR = DriveItem(item_id="old", name="_Transcripts (old)", path="Old/_Transcripts", is_folder=True)


def _file(item_id, path, mime="text/markdown"):
    return DriveItem(item_id=item_id, name=path.split("/")[-1], path=path, is_folder=False, size_bytes=10, mime_type=mime)


def _folder(item_id, path):
    return DriveItem(item_id=item_id, name=path.split("/")[-1], path=path, is_folder=True)


@pytest.fixture
def items():
    return {"mr": MIRROR, "old": OLD_MIRROR}


@pytest.fixture
def drive(items):
    d = AsyncMock(spec=UserDrivePort)
    d.get_root.return_value = ROOT

    async def get_item(handle, item_id):
        if item_id not in items:
            raise DriveItemNotFoundError(item_id)
        return items[item_id]

    d.get_item.side_effect = get_item
    d.list_children.return_value = []
    return d


@pytest.fixture
async def mirror():
    repo = InMemoryDriveMirrorRepository()
    await repo.save_root(MirrorRoot(user_id="u1", drive_identity=DRIVE, root_item_id="mr", root_name="_Transcripts",
                                    previous_root_ids=("old",)))
    return repo


def _connections() -> DriveConnectionService:
    """The connection service the constructor takes; the guard reads only the handle and the drive."""
    return DriveConnectionService(FakeDriveConnectionRepository(), AsyncMock(spec=OAuthCredentialsPort))


@pytest.fixture
def service(drive, mirror):
    return UserDriveService(drive, _connections(), mirror_roots=mirror)


async def test_save_into_the_mirror_refused_before_any_folder_is_created(service, drive):
    for folder in ("_Transcripts", "_transcripts/Archive/2026-10-01", "_Transcripts/new folder"):
        with pytest.raises(DriveMirrorWriteRefused):
            await service.save(H, b"x", "a.txt", "text/plain", folder)
    drive.create_folder.assert_not_called()
    drive.upload.assert_not_called()


async def test_append_replace_delete_of_a_mirror_file_refused(service, drive, items):
    items["f"] = _file("f", "_Transcripts/Archive/2026-10-01/call.md")
    with pytest.raises(DriveMirrorWriteRefused):
        await service.append_text(H, "f", "x")
    with pytest.raises(DriveMirrorWriteRefused):
        await service.replace_with(H, "f", b"x", "text/plain")
    with pytest.raises(DriveMirrorWriteRefused):
        await service.delete(H, "f")
    drive.replace_content.assert_not_called()
    drive.delete.assert_not_called()


async def test_move_into_out_of_and_within_the_mirror_refused(service, drive, items):
    items["f"] = _file("f", "_Transcripts/call.md")
    items["g"] = _file("g", "Docs/notes.txt", "text/plain")
    with pytest.raises(DriveMirrorWriteRefused):      # out of
        await service.move(H, "f", "Docs", None)
    with pytest.raises(DriveMirrorWriteRefused):      # into
        await service.move(H, "g", "_Transcripts/Archive", None)
    with pytest.raises(DriveMirrorWriteRefused):      # within (a rename)
        await service.move(H, "f", None, "other.md")
    drive.move.assert_not_called()
    drive.create_folder.assert_not_called()


async def test_delete_or_move_of_a_folder_containing_the_root_refused(service, drive, items):
    items["wrap"] = _folder("wrap", "Old")
    items["mr"] = _folder("mr", "Old/_Transcripts")
    with pytest.raises(DriveMirrorWriteRefused):
        await service.delete(H, "wrap")
    with pytest.raises(DriveMirrorWriteRefused):
        await service.move(H, "wrap", "Elsewhere", None)
    drive.delete.assert_not_called()
    drive.move.assert_not_called()


async def test_delete_or_move_of_the_root_itself_refused(service, drive):
    with pytest.raises(DriveMirrorWriteRefused):
        await service.delete(H, "mr")
    with pytest.raises(DriveMirrorWriteRefused):
        await service.move(H, "mr", "Docs", None)


async def test_lowercase_path_still_refused(service, drive, items):
    items["f"] = _file("f", "_TRANSCRIPTS/Call.md")
    with pytest.raises(DriveMirrorWriteRefused):
        await service.save(H, b"x", "a.txt", "text/plain", "_transcripts")
    with pytest.raises(DriveMirrorWriteRefused):
        await service.delete(H, "f")


async def test_nfd_path_still_refused(drive, mirror, items):
    items["mr"] = _folder("mr", "Caf\u00e9 Записи")           # NFC on the drive
    svc = UserDriveService(drive, _connections(), mirror_roots=mirror)
    with pytest.raises(DriveMirrorWriteRefused):
        await svc.ensure_folder(H, "CAFE\u0301 записи/sub")  # NFD, other case, from the chat


async def test_create_folder_under_the_mirror_refused(service, drive):
    with pytest.raises(DriveMirrorWriteRefused):
        await service.ensure_folder(H, "_Transcripts/Mine")
    drive.create_folder.assert_not_called()


async def test_previous_root_refused(service, drive, items):
    items["f"] = _file("f", "Old/_Transcripts/Archive/call.md")
    with pytest.raises(DriveMirrorWriteRefused):
        await service.delete(H, "f")
    with pytest.raises(DriveMirrorWriteRefused):
        await service.save(H, b"x", "a.txt", "text/plain", "old/_transcripts")
    with pytest.raises(DriveMirrorWriteRefused):
        await service.delete(H, "old")


async def test_previous_root_that_is_gone_is_skipped(drive, mirror, items):
    del items["old"]
    items["g"] = _file("g", "Docs/notes.txt", "text/plain")
    svc = UserDriveService(drive, _connections(), mirror_roots=mirror)
    await svc.delete(H, "g")
    drive.delete.assert_awaited_once()


async def test_listing_and_open_allowed(service, drive, items):
    items["mr"] = MIRROR
    drive.list_children.return_value = [_file("f", "_Transcripts/call.md")]
    out = await service.list_folder(H, None)
    assert out.folder is not None
    assert (await service.get_item(H, "mr")).item_id == "mr"
    await service.list_folder_by_id(H, "mr")


async def test_ordinary_writes_unaffected(service, drive, items):
    items["g"] = _file("g", "Docs/notes.txt", "text/plain")
    drive.upload.return_value = _file("n", "Inbox/a.txt", "text/plain")
    await service.save(H, b"x", "a.txt", "text/plain", None)
    await service.delete(H, "g")
    drive.delete.assert_awaited_once()


async def test_no_root_no_extra_call(drive, items):
    svc = UserDriveService(drive, _connections(), mirror_roots=InMemoryDriveMirrorRepository())
    items["g"] = _file("g", "Docs/notes.txt", "text/plain")
    await svc.delete(H, "g")
    assert [c.args[1] for c in drive.get_item.await_args_list] == ["g"]


async def test_no_mirror_repository_no_extra_call(drive, items):
    svc = UserDriveService(drive, _connections())
    items["g"] = _file("g", "Docs/notes.txt", "text/plain")
    await svc.delete(H, "g")
    assert [c.args[1] for c in drive.get_item.await_args_list] == ["g"]

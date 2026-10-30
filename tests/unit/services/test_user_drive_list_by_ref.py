"""`UserDriveService.list_folder_by_id` (USER_DRIVE_INDEXING_RFC §26.2, Δ55): `list_files_in_drive` takes a folder's
`drive:` ref as search returns them; the folder is read by id, a file ref is refused, a "not found" propagates."""
from unittest.mock import AsyncMock

import pytest

from src.domain.user_drive import DriveItem, DriveItemNotFoundError, DrivePathError
from src.ports.user_drive_port import UserDrivePort
from src.services.user_drive_service import LIST_LIMIT, UserDriveService
from tests.support.drive_connections import connection_service
from tests.support.drive_handles import drive_handle

H = drive_handle("u")
FOLDER = DriveItem(item_id="d1", name="Shared", path="Shared", is_folder=True)


def _service(item=FOLDER, children=()):
    drive = AsyncMock(spec=UserDrivePort)
    drive.get_item.return_value = item
    drive.list_children.return_value = list(children)
    return UserDriveService(drive, connection_service()), drive


async def test_lists_folders_first_by_name():
    kids = [DriveItem(item_id="b", name="b.pdf", path="Shared/b.pdf", is_folder=False),
            DriveItem(item_id="z", name="Zeta", path="Shared/Zeta", is_folder=True),
            DriveItem(item_id="a", name="A.pdf", path="Shared/A.pdf", is_folder=False)]
    svc, drive = _service(children=kids)
    out = await svc.list_folder_by_id(H, "d1")
    assert out.folder == FOLDER and [i.item_id for i in out.items] == ["z", "a", "b"] and not out.truncated
    drive.list_children.assert_awaited_once_with(H, "d1")


async def test_caps_the_listing():
    kids = [DriveItem(item_id=f"f{n:03d}", name=f"f{n:03d}.txt", path=f"Shared/f{n:03d}.txt", is_folder=False)
            for n in range(LIST_LIMIT + 1)]
    svc, _ = _service(children=kids)
    out = await svc.list_folder_by_id(H, "d1")
    assert len(out.items) == LIST_LIMIT and out.truncated


async def test_a_file_ref_is_refused():
    svc, drive = _service(item=DriveItem(item_id="f1", name="a.pdf", path="a.pdf", is_folder=False))
    with pytest.raises(DrivePathError):
        await svc.list_folder_by_id(H, "f1")
    drive.list_children.assert_not_called()


async def test_not_found_propagates():
    svc, drive = _service()
    drive.get_item.side_effect = DriveItemNotFoundError("d1")
    with pytest.raises(DriveItemNotFoundError):
        await svc.list_folder_by_id(H, "d1")

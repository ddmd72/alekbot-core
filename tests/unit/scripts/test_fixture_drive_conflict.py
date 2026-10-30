"""FixtureDrive honours the upload conflict modes, child_by_name, area_path, ctag and created_at (Task 18)."""
import pytest

from scripts.validation.drive_eval.fixture_drive import AREA_PATH, ROOT_ID, FixtureDrive
from src.domain.user_drive import DriveItemNotFoundError, DriveNameConflictError

_MANIFEST = {"items": [{"path": "_Transcripts", "folder": True},
                       {"path": "_Transcripts/a.md", "mime": "text/markdown", "size": 3}]}


def _drive():
    return FixtureDrive(_MANIFEST)


async def _folder_id(drive):
    return (await drive.list_children("u", ROOT_ID))[0].item_id


async def test_fixture_drive_honours_each_mode():
    drive = _drive()
    folder = await _folder_id(drive)
    first = await drive.child_by_name("u", folder, "a.md")
    assert first is not None

    with pytest.raises(DriveNameConflictError):
        await drive.upload("u", folder, "a.md", b"x", "text/markdown", conflict="fail")

    renamed = await drive.upload("u", folder, "a.md", b"x", "text/markdown")
    assert renamed.name == "a 1.md" and renamed.item_id != first.item_id

    replaced = await drive.upload("u", folder, "a.md", b"new", "text/markdown", conflict="replace")
    assert replaced.item_id == first.item_id and await drive.download("u", first.item_id) == b"new"
    assert replaced.content_version and replaced.content_version != first.content_version

    created = await drive.upload("u", folder, "b.md", b"b", "text/markdown", conflict="fail")
    assert created.path == "_Transcripts/b.md"
    assert "_Transcripts/b.md" in drive.paths()


async def test_child_by_name_and_area_path():
    drive = _drive()
    folder = await _folder_id(drive)
    assert await drive.child_by_name("u", folder, "nope.md") is None
    with pytest.raises(DriveItemNotFoundError):
        await drive.child_by_name("u", "missing", "a.md")
    assert await drive.area_path("u") == AREA_PATH


async def test_every_item_has_ctag_and_created_at():
    drive = _drive()
    folder = await _folder_id(drive)
    change = await drive.child_by_name("u", folder, "a.md")
    assert change.created_at is not None and change.content_version
    item = await drive.get_item("u", change.item_id)
    assert item.content_version == change.content_version

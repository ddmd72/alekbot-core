"""Wire — items and their neutral fields (GOOGLE_DRIVE_RFC §3.4, GE-2, GE-3, GE-5, GE-20, design Δ15). A binary's
change key is its `headRevisionId`, `md5:<md5>` without one; its content identity is `md5`; a native has none. Access
comes from `capabilities`; a native never takes content writes. A photo's EXIF time reads as UTC. A shortcut is an alias
of its target's identity and is never followed. Identities are `google_drive:<fileId>`, with no account part."""
from datetime import datetime, timezone

import pytest

from src.adapters.google.google_drive_mapping import parse_exif_time
from src.domain.drive_content_identity import ContentIdentity
from src.domain.drive_media_facts import DriveMediaFacts
from tests.support.google_drive_fake import DOC, WEB, google_setup


@pytest.fixture
async def setup(monkeypatch):
    return await google_setup(monkeypatch)


async def test_a_binary_change_key_is_its_head_revision(setup):
    node = setup.a.add_file(setup.a.root.id, "Hamburg talk.mp4", b"talk", mime="video/mp4")
    item = await setup.adapter.get_item(setup.handle, node.id)
    assert item.content_version == node.head and node.head.startswith("rev-")
    await setup.adapter.move(setup.handle, node.id, new_name="Hamburg talk 2026.mp4")
    renamed = await setup.adapter.get_change(setup.handle, node.id)
    assert renamed.content_version == item.content_version              # a rename leaves it


async def test_without_a_head_revision_the_key_is_the_md5(setup):
    node = setup.a.add_file(setup.a.root.id, "Hamburg talk.mp4", b"talk")
    node.head = None
    change = await setup.adapter.get_change(setup.handle, node.id)
    assert change.content_version == f"md5:{node.md5}"


async def test_a_binary_has_an_md5_identity_and_a_native_none(setup):
    binary = setup.a.add_file(setup.a.root.id, "Hamburg talk.mp4", b"talk")
    native = setup.a.add_native(setup.a.root.id, "brief.docx", DOC, exports={"text/markdown": b"b"}, size=40)
    assert (await setup.adapter.get_item(setup.handle, binary.id)).content_identity == \
        ContentIdentity("md5", binary.md5, len(b"talk"))
    assert (await setup.adapter.get_change(setup.handle, native.id)).content_identity is None


@pytest.mark.parametrize("caps,field,expected", [
    ({"canDownload": False}, "can_download", False),
    ({"canModifyContent": False}, "can_write_content", False),
    ({"canRename": True, "canMoveItemWithinDrive": False}, "can_rename_move", False),
    ({"canRename": False, "canMoveItemWithinDrive": True}, "can_rename_move", False),
    ({"canTrash": False}, "can_delete", False),
    ({"canAddChildren": False}, "can_add_children", False),
    ({}, "can_rename_move", True),
])
async def test_access_comes_from_capabilities(setup, caps, field, expected):
    node = setup.a.add_file(setup.a.root.id, "Q3 budget.xlsx", b"q3", caps=caps)
    item = await setup.adapter.get_item(setup.handle, node.id)
    assert getattr(item.access, field) is expected


async def test_a_native_never_takes_content_writes(setup):
    node = setup.a.add_native(setup.a.root.id, "brief.docx", DOC, exports={"text/markdown": b"b"},
                              caps={"canModifyContent": True})
    item = await setup.adapter.get_item(setup.handle, node.id)
    assert item.access.can_write_content is False and item.access.can_rename_move is True


async def test_a_photo_time_reads_as_utc_and_an_unreadable_one_as_none(setup):
    photo = setup.a.add_file(setup.a.root.id, "garden.jpg", b"p", mime="image/jpeg",
                             image={"time": "2026:06:01 18:30:05", "cameraMake": "Acme", "width": 4000,
                                    "height": "3000"})
    odd = setup.a.add_file(setup.a.root.id, "odd.jpg", b"p", mime="image/jpeg", image={"time": "2026-06-01"})
    media = (await setup.adapter.get_item(setup.handle, photo.id)).media
    assert media.taken_at == datetime(2026, 6, 1, 18, 30, 5, tzinfo=timezone.utc)
    assert media.taken_at.tzinfo is timezone.utc
    assert (media.camera_make, media.image_width, media.image_height) == ("Acme", 4000, 3000)
    assert (await setup.adapter.get_change(setup.handle, odd.id)).media.taken_at is None
    assert parse_exif_time(None) is None and parse_exif_time("0000:00:00 00:00:00") is None


async def test_a_shortcut_is_an_alias_never_followed(setup, tmp_path):
    target = setup.a.add_file(setup.a.root.id, "Q3 budget.xlsx", b"q3")
    shortcut = setup.a.add_shortcut(setup.a.root.id, "Q3 budget", target=target.id)
    setup.fake.requests.clear()
    item = await setup.adapter.get_item(setup.handle, shortcut.id)
    change = await setup.adapter.get_change(setup.handle, shortcut.id)
    assert item.media.alias_of == change.media.alias_of == f"google_drive:{target.id}"
    assert (item.mime_type, item.content_version, item.size_bytes, item.access.can_download) == ("", "", 0, False)
    assert not any(target.id in r.url for r in setup.fake.requests)
    assert setup.fake.calls("media") == [] and setup.fake.calls("export") == []


async def test_identities_are_the_file_id_in_the_id_space(setup):
    node = setup.a.add_file(setup.a.root.id, "brief.docx", b"b")
    item = await setup.adapter.get_item(setup.handle, node.id)
    change = await setup.adapter.get_change(setup.handle, node.id)
    assert item.identity == change.identity == f"google_drive:{node.id}"
    assert "perm-a" not in item.identity and item.drive_identity == "google_drive:account:perm-a"


async def test_neutral_fields_of_a_file_and_a_folder(setup):
    folder = setup.a.add_folder(setup.a.root.id, "Projects")
    node = setup.a.add_file(folder.id, "Hamburg talk.mp4", b"talk", mime="video/mp4")
    item = await setup.adapter.get_item(setup.handle, node.id)
    change = await setup.adapter.get_change(setup.handle, node.id)
    assert (item.mime_type, item.size_bytes, item.web_url) == ("video/mp4", 4, f"{WEB}/{node.id}")
    assert item.modified_at == datetime.fromisoformat(node.modified.replace("Z", "+00:00"))
    assert change.created_at == datetime.fromisoformat(node.created.replace("Z", "+00:00"))
    assert change.fs_modified_at is None and change.child_count == 0 and change.parent_id == folder.id
    folder_item = await setup.adapter.get_item(setup.handle, folder.id)
    assert (folder_item.is_folder, folder_item.mime_type, folder_item.content_version, folder_item.size_bytes,
            folder_item.child_count, folder_item.content_identity) == (True, "", "", 0, 0, None)
    assert folder_item.media == DriveMediaFacts()


async def test_a_trashed_item_is_not_found_by_get_item_and_deleted_by_get_change(setup):
    node = setup.a.add_file(setup.a.root.id, "brief.docx", b"b")
    setup.a.trash(node)
    change = await setup.adapter.get_change(setup.handle, node.id)
    assert change.deleted and change.identity == f"google_drive:{node.id}"
    with pytest.raises(FileNotFoundError):
        await setup.adapter.get_item(setup.handle, node.id)

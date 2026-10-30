"""FixtureDrive: the eval's UserDrivePort over tree.json (USER_DRIVE_INDEXING_RFC §4, §17.8; S0e facets)."""
import pytest

from scripts.validation.drive_eval.fixture_drive import DRIVE_ID, ROOT_ID, FixtureDrive, item_id_for
from src.adapters.microsoft.onedrive_adapter import onedrive_drive_identity
from src.domain.drive_content_identity import ContentIdentity
from src.domain.drive_media_facts import DriveMediaFacts
from src.domain.user_drive import DriveItemNotFoundError, DriveResyncRequired

from .drive_eval_samples import MANIFEST

USER = "u"


def test_ids_are_stable_synthetic_hashes_of_the_path():
    a, b = FixtureDrive(MANIFEST), FixtureDrive(MANIFEST)
    assert a.paths() == b.paths()
    for path in a.paths():
        assert a.path_of(item_id_for(path)) == path
        assert "!s" not in item_id_for(path)


async def test_full_enumeration_is_parent_first_then_deltas_carry_only_the_root():
    drive = FixtureDrive(MANIFEST, page_size=100)
    seen, cursor, pages = {ROOT_ID}, None, 0
    while True:
        page = await drive.get_changes(USER, cursor)
        pages += 1
        assert page.full_enumeration and page.drive_identity == onedrive_drive_identity(DRIVE_ID)
        assert page.root_item_id == ROOT_ID
        if pages == 1:
            assert page.changes[0].item_id == ROOT_ID and page.changes[0].is_folder   # S0a: the root, every round
        for change in page.changes:
            if change.item_id != ROOT_ID:
                assert change.parent_id in seen, drive.path_of(change.item_id)
                seen.add(change.item_id)
        cursor = page.cursor
        if page.complete:
            break
    assert pages == 6 and len(seen) - 1 == len(drive.paths()) == 541
    delta = await drive.get_changes(USER, cursor)
    assert delta.complete and not delta.full_enumeration and [c.item_id for c in delta.changes] == [ROOT_ID]


async def test_unknown_cursor_asks_for_a_resync():
    with pytest.raises(DriveResyncRequired):
        await FixtureDrive(MANIFEST).get_changes(USER, "something-else")


async def test_generated_items_implicit_folders_and_s0e_facets():
    drive = FixtureDrive(MANIFEST)
    note = await drive.get_change(USER, item_id_for("Vault/note-001.md"))
    assert note.content_identity == ContentIdentity("quickxor", "note1=", note.size_bytes)
    assert note.media == DriveMediaFacts() and note.mime_type == "text/markdown"
    photo = await drive.get_change(USER, item_id_for("Photos/Rome 2025/IMG_0001.HEIC"))
    media = photo.media   # the photo, location and image facets, and nothing else
    assert media.taken_at is not None and media.camera_make and media.location is not None
    assert media.image_width and media.image_height and photo.content_version
    assert media == DriveMediaFacts(taken_at=media.taken_at, camera_make=media.camera_make,
                                    camera_model=media.camera_model, latitude=media.latitude,
                                    longitude=media.longitude, image_width=media.image_width,
                                    image_height=media.image_height)
    lease = await drive.get_change(USER, item_id_for("Inbox/Lease agreement — Porto.pdf"))
    assert (lease.content_identity.family, lease.content_identity.digest) == ("quickxor", "bGVhc2U=")
    inbox = await drive.get_change(USER, item_id_for("Inbox"))
    assert inbox.is_folder and inbox.child_count == 1 and inbox.parent_id == ROOT_ID
    mirror = await drive.get_change(USER, item_id_for("Dumps/site-mirror"))
    assert mirror.child_count == 520 and mirror.content_version == ""


async def test_single_reads():
    drive = FixtureDrive(MANIFEST)
    # the UserDrivePort contract of USER_DRIVE_INDEXING_RFC §26.2 (Δ48): "not found" by id raises, never a deletion
    with pytest.raises(DriveItemNotFoundError):
        await drive.get_change(USER, "fx-unknown")
    known = await drive.get_change(USER, item_id_for("Dumps/site-mirror"))
    assert known.item_id == item_id_for("Dumps/site-mirror") and known.is_folder and not known.deleted
    mirror = item_id_for("Dumps/site-mirror")
    first = await drive.list_children_changes(USER, mirror, limit=5)
    assert [k.name for k in first.changes] == [f"page-{n:03d}.bin" for n in range(5)] and first.next_cursor
    second = await drive.list_children_changes(USER, mirror, limit=5, cursor=first.next_cursor)
    assert [k.name for k in second.changes] == [f"page-{n:03d}.bin" for n in range(5, 10)]
    names, cursor = [], None
    while True:
        page = await drive.list_children_changes(USER, mirror, limit=200, cursor=cursor)
        names += [k.name for k in page.changes]
        cursor = page.next_cursor
        if cursor is None:
            break
    assert len(names) == 520 == len(set(names))
    with pytest.raises(DriveItemNotFoundError):
        await drive.list_children_changes(USER, "fx-unknown", limit=5)
    connection = await drive.connection(USER)
    assert connection.connected and not connection.needs_reconnect and connection.drive_id == DRIVE_ID

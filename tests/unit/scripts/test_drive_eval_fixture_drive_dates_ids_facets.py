"""FixtureDrive dates, cross-process id stability and pass-through facets."""
from datetime import datetime, timedelta, timezone

from scripts.validation.drive_eval.fixture_drive import DEFAULT_FS_MODIFIED, FixtureDrive, item_id_for
from src.domain.drive_media_facts import DriveMediaFacts

from .drive_eval_samples import MANIFEST

USER = "u"
_LOCAL = {
    "items": [
        {"path": "Media/clip.mp4", "folder": False, "mime": "video/mp4", "size": 7340032,
         "fs_modified": "2025-06-02T08:30:00Z",
         "facets": {"video": {"duration": 61000, "width": 1920, "height": 1080}}},
        {"path": "Messenger/IMG-20250601-WA0001.jpg", "folder": False, "mime": "image/jpeg", "size": 123457,
         "fs_modified": "2025-06-01T12:00:00Z", "facets": {"photo": {}, "image": {"width": 800, "height": 600}}},
    ],
    "generate": [
        {"folder": "Archive/Invoices 2024", "pattern": "INV-{n:03d}.pdf", "count": 2, "mime": "application/pdf",
         "size": 50, "fs_modified": "2024-03-05T08:00:00Z", "facets_template": {}},
        {"folder": "Dated by default", "pattern": "file-{n}.txt", "count": 1, "mime": "text/plain", "size": 5,
         "facets_template": {}},
    ],
}


def _assert_utc(value: datetime, expected: datetime) -> None:
    assert value.tzinfo is not None and value.utcoffset() == timedelta(0)
    assert value == expected


async def test_explicit_item_keeps_its_own_date_tz_aware():
    change = await FixtureDrive(MANIFEST).get_change(USER, item_id_for("Scans/Scan_0012.pdf"))
    expected = datetime(2026, 4, 1, 10, tzinfo=timezone.utc)
    _assert_utc(change.fs_modified_at, expected)
    _assert_utc(change.modified_at, expected)


async def test_generate_entry_date_and_the_default_fallback():
    drive = FixtureDrive(_LOCAL)
    for n in range(2):
        invoice = await drive.get_change(USER, item_id_for(f"Archive/Invoices 2024/INV-{n:03d}.pdf"))
        _assert_utc(invoice.fs_modified_at, datetime(2024, 3, 5, 8, tzinfo=timezone.utc))
        _assert_utc(invoice.modified_at, datetime(2024, 3, 5, 8, tzinfo=timezone.utc))
    default = await drive.get_change(USER, item_id_for("Dated by default/file-0.txt"))
    assert DEFAULT_FS_MODIFIED == "2026-01-15T09:00:00Z"
    _assert_utc(default.fs_modified_at, datetime(2026, 1, 15, 9, tzinfo=timezone.utc))


def test_item_id_is_a_fixed_literal_across_processes():
    """A salted hash() would pass a same-process check; the literal pins sha256 of the path."""
    assert item_id_for("Scans/Scan_0012.pdf") == "fx-0c67b1f163962fbf"


async def test_video_facet_empty_photo_facet_and_file_size_pass_through():
    drive = FixtureDrive(_LOCAL)
    clip = await drive.get_change(USER, item_id_for("Media/clip.mp4"))
    assert clip.media == DriveMediaFacts(video_duration_ms=61000, video_width=1920, video_height=1080)
    assert clip.size_bytes == 7340032
    jpeg = await drive.get_change(USER, item_id_for("Messenger/IMG-20250601-WA0001.jpg"))
    assert jpeg.media == DriveMediaFacts(image_width=800, image_height=600)   # an empty photo facet adds no fact
    assert jpeg.size_bytes == 123457

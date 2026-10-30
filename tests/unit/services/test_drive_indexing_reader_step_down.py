"""A text image whose big rendition is over the vision cap is read from a smaller one; only when none fits is it
demoted, and then as too large, not as "no text" (USER_DRIVE_INDEXING_RFC §24.5)."""
import pytest

from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import Reason, RecordState, TriageVerdict
from src.domain.drive_index_params import READER_RENDITION_EDGE_PX, READER_RENDITION_STEPS
from src.domain.drive_index_run import READER_IMAGE_MAX_BYTES
from src.domain.user_drive import DriveThumbnailTooLarge

from .drive_content_world import JPEG, ContentWorld, admitted  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("admitted")


@pytest.fixture
def w():
    return ContentWorld()


def _read():
    return ExtractionOutcome(status=ExtractionStatus.TEXT, text="Citizen card SAMPLE. Valid until 07 05 2031.", pages_total=1,
                             pages_sent=1, model_calls=1)


def _over_cap(w, monkeypatch, sizes, otherwise=None):
    original = w.drive.get_thumbnail

    async def renditions(user_id, item_id, size):
        if size in sizes:
            w.drive.thumbnail_calls.append((item_id, size))
            raise DriveThumbnailTooLarge(size, 1)
        if otherwise is not None and size in READER_RENDITION_STEPS:
            w.drive.thumbnail_calls.append((item_id, size))
            return otherwise
        return await original(user_id, item_id, size)
    monkeypatch.setattr(w.drive, "get_thumbnail", renditions)


async def test_over_cap_big_rendition_steps_down_and_the_reader_runs(w, monkeypatch):
    w.triage.default = TriageVerdict.TEXT
    w.reader.outcomes["scan.png"] = _read()
    img = w.add_image("scan.png", thumbnail=JPEG)
    _over_cap(w, monkeypatch, {READER_RENDITION_EDGE_PX})
    await w.settle()
    assert w.rec(img).state is RecordState.INDEXED
    assert [s for i, s in w.drive.thumbnail_calls if i == img and s in READER_RENDITION_STEPS] == \
        list(READER_RENDITION_STEPS[:2])
    assert img not in w.drive.downloads


async def test_every_size_over_cap_demotes_as_too_large(w, monkeypatch):
    w.triage.default = TriageVerdict.TEXT
    img = w.drive.add_file("scan.heic", b"\x00\x00\x00\x18ftypheic", thumbnail=JPEG, mime_type="image/heic")
    _over_cap(w, monkeypatch, set(READER_RENDITION_STEPS))
    await w.settle()
    rec = w.rec(img)
    assert rec.reason is Reason.TOO_LARGE and w.reader.requests == [] and w.drive.downloads == []


async def test_every_size_over_cap_still_reads_a_small_original(w, monkeypatch):
    w.triage.default = TriageVerdict.TEXT
    w.reader.outcomes["scan.jpg"] = _read()
    img = w.add_image("scan.jpg", thumbnail=JPEG)
    _over_cap(w, monkeypatch, set(READER_RENDITION_STEPS))
    await w.settle()
    assert img in w.drive.downloads and w.rec(img).state is RecordState.INDEXED


async def test_every_size_over_cap_and_a_big_original_is_too_large(w, monkeypatch):
    w.triage.default = TriageVerdict.TEXT
    img = w.drive.add_file("big.png", b"\x89PNG" + b"\x00" * READER_IMAGE_MAX_BYTES, thumbnail=JPEG,
                           mime_type="image/png")
    _over_cap(w, monkeypatch, set(READER_RENDITION_STEPS))
    await w.settle()
    assert w.rec(img).reason is Reason.TOO_LARGE and w.drive.downloads == []


async def test_no_rendition_keeps_the_no_text_demotion(w, monkeypatch):
    w.triage.default = TriageVerdict.TEXT
    img = w.drive.add_file("scan.heic", b"\x00\x00\x00\x18ftypheic", thumbnail=JPEG, mime_type="image/heic")
    original = w.drive.get_thumbnail

    async def renditions(user_id, item_id, size):
        return None if size in READER_RENDITION_STEPS else await original(user_id, item_id, size)
    monkeypatch.setattr(w.drive, "get_thumbnail", renditions)
    await w.settle()
    assert w.rec(img).reason is Reason.NO_TEXT


async def test_an_over_cap_triage_thumbnail_counts_as_no_rendition(w, monkeypatch):
    img = w.add_image("photo.jpg", thumbnail=JPEG)
    original = w.drive.get_thumbnail

    async def renditions(user_id, item_id, size):
        if size == "large":
            raise DriveThumbnailTooLarge(size, 1)
        return await original(user_id, item_id, size)
    monkeypatch.setattr(w.drive, "get_thumbnail", renditions)
    await w.settle()
    assert w.rec(img).triage_verdict is None

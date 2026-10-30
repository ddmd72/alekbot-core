"""An item the provider says is gone during content work (USER_DRIVE_INDEXING_RFC §7.6, §8.2, §26.2): a download, a
reader rendition or a triage thumbnail that raises DriveItemNotFoundError re-reads the item by id at once; it is never
read as "no thumbnail" or "no content". Since §26.2 (D33, Δ48) a 404 on that re-read marks the record inaccessible and
never deletes it: existence is decided by delta, and the tombstone that follows a real deletion removes it.

The world's drive follows the §26.2 port contract (`get_change` raises DriveItemNotFoundError for an item it does not
have, never a deletion)."""
from datetime import timedelta

import pytest

from src.domain.drive_index import RecordState, TriageVerdict, inaccessible
from src.domain.drive_index_params import READER_RENDITION_EDGE_PX
from src.domain.user_drive import DriveItemNotFoundError

from .drive_content_world import admitted, prose  # noqa: F401  (fixture)
from .drive_inaccessible_world import ClosedWorld

pytestmark = pytest.mark.usefixtures("admitted")


@pytest.fixture
def w():
    return ClosedWorld()


def vanish(w, item_id):
    """Gone from the drive, its tombstone not in the feed yet."""
    w.drive.items.pop(item_id)


async def test_a_vanished_document_is_marked_then_removed_with_its_chunks_by_its_tombstone(w):
    doc = w.add_doc("a.md", prose(50))
    await w.settle()
    readable_before = w.readable(doc)
    assert readable_before
    w.drive.edit(doc, prose(60).encode(), modified_at=w.clock.now - timedelta(hours=1))
    real = w.drive.download_to_file

    async def gone(user_id, item_id, dest_path, max_bytes):
        vanish(w, item_id)   # deleted between the feed and the download; its tombstone is not in the feed yet
        return await real(user_id, item_id, dest_path, max_bytes)
    w.drive.download_to_file = gone
    await w.settle()
    # a 404 is not a deletion (§26.2, Δ48): marked, never deleted; the readable generation stays (Δ52); no attempt
    rec = w.rec(doc)
    assert rec is not None and inaccessible(rec) and w.summary_total("inaccessible") == 1
    assert w.summary_total("gone") == 0
    assert (rec.attempts, rec.attempt_started_at, rec.reason) == (0, None, None)
    assert w.readable(doc) == readable_before
    assert w.parser.requests[-1].name == "a.md" and len(w.parser.requests) == 1
    # the tombstone delta brings for the real deletion removes the record and its chunks (§7.6)
    w.drive.delete(doc)
    await w.settle()
    assert w.rec(doc) is None and w.chunks(doc) == []
    assert len(w.parser.requests) == 1


async def test_a_vanished_image_is_not_a_photograph_without_a_caption(w):
    img = w.add_image("IMG_1.jpg")
    other = w.add_image("IMG_2.jpg")
    real = w.drive.get_thumbnail

    async def thumbnail(user_id, item_id, size):
        if item_id == img:
            vanish(w, item_id)   # observed, then deleted before its thumbnail is fetched
        return await real(user_id, item_id, size)
    w.drive.get_thumbnail = thumbnail
    await w.settle()
    # marked, never deleted on the 404 (§26.2, Δ48), and never triaged as a photograph without a caption
    rec = w.rec(img)
    assert rec is not None and inaccessible(rec) and rec.triage_verdict is None
    assert w.rec(other).triage_verdict is TriageVerdict.PHOTO
    assert [i.item_id for batch in w.triage.calls for i in batch] == [other]
    assert w.summary_total("inaccessible") == 1 and w.summary_total("gone") == 0
    w.drive.delete(img)   # its tombstone removes it (§7.6)
    await w.settle()
    assert w.rec(img) is None
    assert [i.item_id for batch in w.triage.calls for i in batch] == [other]


async def test_a_vanished_rendition_for_the_reader_re_reads_and_marks(w):
    w.triage.default = TriageVerdict.TEXT
    img = w.add_image("receipt.jpg")
    real = w.drive.get_thumbnail

    async def thumbnail(user_id, item_id, size):
        if size == READER_RENDITION_EDGE_PX:
            vanish(w, item_id)
        return await real(user_id, item_id, size)
    w.drive.get_thumbnail = thumbnail
    await w.settle()
    # the item was re-read by id once; its 404 marks it (§26.2, Δ48), and the reader is never called on it
    rec = w.rec(img)
    assert rec is not None and inaccessible(rec) and w.reader.requests == []
    assert w.drive.calls_for(img) == 1 and w.summary_total("inaccessible") == 1
    w.drive.delete(img)   # its tombstone removes it (§7.6)
    await w.settle()
    assert w.rec(img) is None and w.reader.requests == []


async def test_not_found_but_there_after_all_costs_an_attempt(w):
    doc = w.add_doc("a.md", prose(5))
    w.drive.download_errors[doc] = DriveItemNotFoundError(doc)
    await w.settle()
    rec = w.rec(doc)
    assert (rec.state, rec.attempts, rec.attempt_started_at) == (RecordState.PENDING, 1, None)
    w.clock.advance(3600)
    await w.settle()
    assert w.rec(doc).state is RecordState.INDEXED


async def test_a_thumbnail_not_found_but_there_costs_an_attempt(w):
    img = w.add_image("a.jpg")
    real = w.drive.get_thumbnail
    calls = []

    async def flaky(user_id, item_id, size):
        calls.append(item_id)
        if len(calls) == 1:
            raise DriveItemNotFoundError(item_id)
        return await real(user_id, item_id, size)
    w.drive.get_thumbnail = flaky
    await w.settle()
    assert w.rec(img).attempts == 1 and w.rec(img).triage_verdict is None
    w.clock.advance(3600)
    await w.settle()
    assert w.rec(img).triage_verdict is TriageVerdict.PHOTO


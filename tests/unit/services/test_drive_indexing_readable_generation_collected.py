"""Invariant (R3 F2-2, USER_DRIVE_INDEXING_RFC §3.4, §7.4, §7.6): a write that leaves a record without the readable
generation it had collects that generation's chunks, whatever path wrote it — an observation, an index run, a triage
verdict, a missing rendition. No orphaned chunk outlives the tick that dropped it: orphans would take CHUNK_FETCH kNN
slots and count toward the term selectivity test."""
from datetime import timedelta

import pytest

from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import Reason, RecordState, TriageVerdict
from src.domain.drive_index_params import MAX_ATTEMPTS, QUIET, RETRY_CAP

from .drive_content_world import JPEG, ContentWorld, admitted, prose  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("admitted")


def assert_no_orphans(w: ContentWorld) -> None:
    """Every chunk belongs to a stored record and to its readable generation."""
    for (user, item_id, generation, _), _ in w.repo.chunks.items():
        rec = w.rec(item_id)
        assert rec is not None, f"chunk of missing record {item_id}"
        assert generation == rec.indexed_generation, (
            f"{item_id}: chunk of generation {generation}, readable {rec.indexed_generation} ({rec.state})")


@pytest.fixture
def w():
    return ContentWorld()


async def indexed_image(w: ContentWorld, name: str = "receipt.jpg") -> str:
    img = w.add_image(name)
    w.triage.verdicts = {img: (TriageVerdict.TEXT, "")}
    w.reader.outcomes[name] = ExtractionOutcome(status=ExtractionStatus.TEXT, text=prose(30), pages_total=1,
                                                pages_sent=1)
    await w.settle()
    rec = w.rec(img)
    assert rec.state is RecordState.INDEXED and w.readable(img)
    return img


async def test_a_photograph_verdict_on_a_new_version_collects_the_text_chunks(w):
    img = await indexed_image(w)
    w.drive.edit(img, JPEG + b"v2")
    w.triage.verdicts = {img: (TriageVerdict.PHOTO, "a cat on a sofa")}
    w.clock.advance(QUIET.total_seconds() + 1)
    await w.settle()
    rec = w.rec(img)
    assert (rec.state, rec.reason, rec.indexed_generation) == (RecordState.METADATA_ONLY, Reason.PHOTOGRAPH, None)
    assert w.chunks(img) == []
    assert_no_orphans(w)


async def test_a_missing_rendition_that_ends_as_a_photograph_collects_the_text_chunks(w):
    img = await indexed_image(w)
    w.drive.edit(img, JPEG + b"v2")
    del w.drive.thumbnails[img]
    w.clock.advance(QUIET.total_seconds() + 1)
    for _ in range(MAX_ATTEMPTS):
        await w.settle()
        w.clock.advance(int(RETRY_CAP.total_seconds()))
    rec = w.rec(img)
    assert (rec.reason, rec.triage_verdict, rec.indexed_generation) == (Reason.PHOTOGRAPH, TriageVerdict.PHOTO,
                                                                        None)
    assert w.chunks(img) == []
    assert_no_orphans(w)


async def test_an_observation_that_demotes_collects_the_chunks(w):
    doc = w.add_doc("terms.txt", prose(40))
    await w.settle()
    assert w.readable(doc)
    w.drive.rename(doc, "terms.exe")
    await w.settle()
    assert w.rec(doc).indexed_generation is None and w.chunks(doc) == []
    assert_no_orphans(w)


async def test_an_index_run_that_demotes_collects_the_chunks(w):
    doc = w.add_doc("terms.txt", prose(40))
    await w.settle()
    w.drive.edit(doc, b"   ", modified_at=w.clock.now - timedelta(hours=1))
    w.clock.advance(QUIET.total_seconds() + 1)
    await w.settle()
    assert (w.rec(doc).state, w.rec(doc).reason) == (RecordState.METADATA_ONLY, Reason.NO_TEXT)
    assert w.chunks(doc) == []
    assert_no_orphans(w)

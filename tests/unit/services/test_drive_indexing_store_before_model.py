"""The index store is asked before the reader is (final review F4-2; USER_DRIVE_INDEXING_RFC §5.4, §8.2): the chunk-cap
read happens before any model call, so a store that cannot answer it (a missing composite index, an outage) ends the
tick before anything is billed and counts nothing against the file; and a user whose chunks fill the cap has no
reader call at all."""
import pytest

from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import DriveIndexStoreUnavailable, Reason, RecordState
from src.domain.drive_index_params import MAX_CHUNKS_PER_USER

from .drive_content_world import ContentWorld, admitted  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("admitted")

SCAN = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=3)
READ = ExtractionOutcome(status=ExtractionStatus.TEXT, text="Receipt. Total 12.50 EUR. Thank you.", pages_total=3,
                         pages_sent=3, model_calls=1, model_cost=0.002)


@pytest.fixture
def w():
    w = ContentWorld()
    w.parser.outcomes["scan.pdf"] = SCAN
    w.reader.outcomes["scan.pdf"] = READ
    return w


async def test_a_store_that_cannot_count_chunks_ends_the_tick_before_the_reader(w):
    real = w.repo.readable_chunk_total
    broken = {"on": True}

    async def total(user_id):
        if broken["on"]:
            raise DriveIndexStoreUnavailable("readable_chunk_total: FailedPrecondition")
        return await real(user_id)

    w.repo.readable_chunk_total = total
    doc = w.add_doc("scan.pdf", "")
    for _ in range(4):
        result = await w.tick()
        w.clock.advance(3600)
    assert result.status == "error"
    assert w.reader.requests == []   # nothing billed
    rec = w.rec(doc)
    assert (rec.state, rec.attempts, rec.attempt_started_at) == (RecordState.PENDING, 0, None)
    broken["on"] = False
    await w.settle()
    assert w.rec(doc).state is RecordState.INDEXED and len(w.reader.requests) == 1


async def test_no_chunk_headroom_means_no_reader_call(w):
    async def full(user_id):
        return MAX_CHUNKS_PER_USER

    w.repo.readable_chunk_total = full
    doc = w.add_doc("scan.pdf", "")
    await w.settle()
    rec = w.rec(doc)
    assert w.reader.requests == []
    assert (rec.state, rec.reason) == (RecordState.METADATA_ONLY, Reason.USER_CHUNK_CAP)
    assert rec.chunk_count >= 1   # released only once some headroom is back (§5.4)

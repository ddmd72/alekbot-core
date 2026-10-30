"""Counters and the tick summary for content (USER_DRIVE_INDEXING_RFC §12.1, §10.2 `unread`, §24.9)."""
import pytest

from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
from src.domain.drive_index_params import MAX_ATTEMPTS, RETRY_CAP
from src.domain.drive_search import UNREAD_COUNTER

from .drive_content_world import ContentWorld, admitted, prose  # noqa: F401  (fixture)
from .drive_index_fakes import U_SCOPE

pytestmark = pytest.mark.usefixtures("admitted")


@pytest.fixture
def w():
    return ContentWorld()


async def test_settled_counters_carry_readable_chunks_and_unread(w):
    read = w.add_doc("read.md", prose(150))
    w.parser.outcomes["scan.docx"] = ExtractionOutcome(status=ExtractionStatus.NO_TEXT)
    w.parser.outcomes["broken.md"] = ExtractionOutcome(status=ExtractionStatus.ERROR)
    w.drive.add_file("scan.docx", b"PK\x03\x04")
    w.add_doc("broken.md", "x")
    w.add_image("IMG_1.jpg")                       # a photograph is never unread
    w.drive.add_file("main.py", b"print(1)")       # a kind not admitted is never unread
    for _ in range(MAX_ATTEMPTS + 1):
        await w.settle()
        w.clock.advance(RETRY_CAP.total_seconds())
    await w.settle()
    state = await w.repo.get_sync_state(U_SCOPE)
    assert state.counters["chunks"] == w.rec(read).chunk_count
    assert state.counters[UNREAD_COUNTER] == 2    # no_text and failed
    assert state.counters["indexed"] == 1 and state.counters["failed"] == 1


async def test_zero_content_counters_are_left_out(w):
    w.drive.add_file("main.py", b"print(1)")
    await w.settle()
    state = await w.repo.get_sync_state(U_SCOPE)
    assert "chunks" not in state.counters and UNREAD_COUNTER not in state.counters


async def test_a_pending_document_is_unread_until_indexed(w):
    w.parser.outcomes["scan.pdf"] = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=2)
    w.reader.unavailable = True
    w.add_doc("scan.pdf", "")
    await w.settle()
    w.clock.advance(3600 * 24)   # the model backoff ends, the reader is still out: the tick settles anyway
    await w.settle()
    state = await w.repo.get_sync_state(U_SCOPE)
    assert state.counters[UNREAD_COUNTER] == 1


async def test_the_summary_line_counts_content_work(w, caplog):
    w.parser.outcomes["scan.pdf"] = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=2)
    w.reader.outcomes["scan.pdf"] = ExtractionOutcome(status=ExtractionStatus.TEXT, text=prose(5), pages_total=2,
                                                      pages_sent=2, model_calls=1, model_cost=0.0012)
    w.add_doc("scan.pdf", "")
    w.add_doc("a.md", prose(5))
    w.add_image("IMG_1.jpg")
    caplog.set_level("INFO")
    await w.settle()
    line = next(r.getMessage() for r in caplog.records if "tick user=u end=done" in r.getMessage()
                and "'indexed'" in r.getMessage())
    for key in ("'indexed': 2", "'chunks':", "'embed_ms':", "'model_pages': 2", "'model_calls': 1",
                "'triage_images': 1", "'triage_calls': 1", "model_cost=$0.0012"):
        assert key in line, key

"""The reader in the tick (USER_DRIVE_INDEXING_RFC §9 router, §24.3, §24.5, §24.7, §24.8, §24.9): routing, model
outages that never end the tick, budgets across a month boundary, images read from renditions. Search v2 (§25.4.5):
the page budget is the reader's dollars (the open initial build, then the month's cap) at the per-page estimate."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import DocumentBrief, Priority, Reason, RecordState, TriageVerdict
from src.domain.drive_enrichment import UsageClass
from src.domain.drive_index_params import (
    INITIAL_BUILD_USD, MODEL_BACKOFF_BASE_S, READER_PAGE_USD_ESTIMATE, READER_RENDITION_SIZE, READER_USD_PER_MONTH,
)
from src.domain.drive_index_run import READER_IMAGE_MAX_BYTES

from .drive_content_world import JPEG, PNG, ContentWorld, admitted, prose  # noqa: F401  (fixture)
from .drive_usage_helpers import affordable, close_initial_build, month_calls, month_usd, spend
from .drive_index_fakes import U_SCOPE

pytestmark = pytest.mark.usefixtures("admitted")

SCAN = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=3)


def read(text="Receipt. Total 12.50 EUR. Thank you.", pages=3, **kw):
    return ExtractionOutcome(status=ExtractionStatus.TEXT, text=text, pages_total=pages, pages_sent=pages,
                             model_calls=1, **kw)


@pytest.fixture
def w():
    return ContentWorld()


async def test_a_scan_goes_to_the_reader(w):
    w.parser.outcomes["scan.pdf"] = SCAN
    brief = DocumentBrief(doc_type="receipt", doc_date=datetime(2024, 3, 15, tzinfo=timezone.utc))
    # the text the reader read prints the brief's date, so it is the file's date (Δ60 grades it first)
    w.reader.outcomes["scan.pdf"] = read(text="Receipt 15.03.2024. Total 12.50 EUR. Thank you.", brief=brief,
                                         model_cost=0.002)
    doc = w.add_doc("scan.pdf", "")
    await w.settle()
    rec = w.rec(doc)
    assert (rec.state, rec.extracted_by, rec.brief, rec.effective_date) == \
        (RecordState.INDEXED, "reader@1", replace(brief, date_grade="in_text"), brief.doc_date)
    (request,) = w.reader.requests
    every_page = affordable(INITIAL_BUILD_USD + READER_USD_PER_MONTH, READER_PAGE_USD_ESTIMATE)
    assert (request.kind, request.max_model_pages, request.account_id) == ("pdf", every_page, "a")
    # the dollars the reader spent, priced by the runner, are in the month's document and the initial build paid them
    assert await month_usd(w.repo, UsageClass.READER, "2026-10") == pytest.approx(0.002)
    assert (await w.repo.get_initial_build("u")).spent_usd == pytest.approx(0.002)
    assert w.summary_total("model_pages") == 3 and w.summary_total("model_calls") == 1
    assert sum(r.model_cost for r in w.results) == pytest.approx(0.002)


async def test_a_brief_date_the_read_text_lacks_is_not_the_files_date(w):
    """Δ60 on the brief path: the reader's brief date, absent from the text it read, stays on the brief graded
    not_in_text and never becomes the effective date."""
    w.parser.outcomes["scan.pdf"] = SCAN
    brief = DocumentBrief(doc_type="receipt", doc_date=datetime(2024, 3, 15, tzinfo=timezone.utc))
    w.reader.outcomes["scan.pdf"] = read(brief=brief, model_cost=0.002)   # the text holds no date
    doc = w.add_doc("scan.pdf", "")
    await w.settle()
    rec = w.rec(doc)
    assert rec.brief == replace(brief, date_grade="not_in_text") and rec.brief.held_date is None
    assert rec.effective_date is not None and rec.effective_date != brief.doc_date


@pytest.mark.parametrize("parser", [
    ExtractionOutcome(status=ExtractionStatus.TEXT, text="1\n2\n3", pages_total=3),            # low density
    ExtractionOutcome(status=ExtractionStatus.TEXT, text="(cid:12)(cid:13)" * 300, pages_total=1),  # garbage
    ExtractionOutcome(status=ExtractionStatus.ERROR),                                            # parser error
    ExtractionOutcome(status=ExtractionStatus.UNSUPPORTED),
])
async def test_router_signals(w, parser):
    w.parser.outcomes["x.pdf"] = parser
    w.reader.outcomes["x.pdf"] = read()
    doc = w.add_doc("x.pdf", "")
    await w.settle()
    assert len(w.reader.requests) == 1 and w.rec(doc).extracted_by == "reader@1"


@pytest.mark.parametrize("parser", [ExtractionOutcome(status=ExtractionStatus.TIMEOUT),
                                    ExtractionOutcome(status=ExtractionStatus.MEMORY),
                                    ExtractionOutcome(status=ExtractionStatus.ENCRYPTED)])
async def test_bombs_and_encryption_never_reach_the_reader(w, parser):
    w.parser.outcomes["x.pdf"] = parser
    w.add_doc("x.pdf", "")
    await w.settle()
    assert w.reader.requests == []


async def test_a_docx_is_never_routed(w):
    w.parser.outcomes["x.docx"] = SCAN
    doc = w.drive.add_file("x.docx", b"PK\x03\x04")
    await w.settle()
    assert w.reader.requests == [] and w.rec(doc).reason is Reason.NO_TEXT


async def test_the_reader_finding_no_text_is_deterministic(w):
    w.parser.outcomes["scan.pdf"] = SCAN
    w.reader.outcomes["scan.pdf"] = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=3, pages_sent=3)
    doc = w.add_doc("scan.pdf", "")
    await w.settle()
    assert w.rec(doc).reason is Reason.NO_TEXT and len(w.reader.requests) == 1


async def test_a_model_outage_defers_model_work_only(w):
    w.parser.outcomes["scan.pdf"] = SCAN
    w.reader.outcomes["scan.pdf"] = read()
    w.reader.unavailable = True
    scan = w.add_doc("scan.pdf", "")
    note = w.add_doc("note.md", prose(10))
    result = await w.settle()
    assert result.status == "done"
    assert w.rec(note).state is RecordState.INDEXED and w.rec(scan).has_card   # parser work and cards go on
    rec = w.rec(scan)
    state = await w.repo.get_sync_state(U_SCOPE)
    assert (rec.state, rec.attempts, rec.attempt_started_at) == (RecordState.PENDING, 0, None)
    assert state.model_failures == 1 and state.model_backoff_until == rec.due_at
    assert state.last_error is None and state.systemic_failures == 0
    w.clock.advance(MODEL_BACKOFF_BASE_S - 1)
    await w.tick()
    assert len(w.reader.requests) == 1   # not before the backoff ends
    w.reader.unavailable = False
    w.clock.advance(1)
    await w.settle()
    state = await w.repo.get_sync_state(U_SCOPE)
    assert w.rec(scan).state is RecordState.INDEXED and (state.model_failures, state.model_backoff_until) == (0, None)


async def test_an_outage_skips_image_reads_without_a_call(w):
    w.triage.verdicts = {}
    w.triage.default = TriageVerdict.TEXT
    w.reader.unavailable = True
    img = w.add_image("receipt.jpg")
    await w.settle()
    calls = len(w.reader.requests)
    second = w.add_image("receipt2.jpg")
    await w.settle()
    assert len(w.reader.requests) == calls   # the backoff holds: no call for the second image
    assert w.rec(img).state is RecordState.PENDING and w.rec(second).triage_verdict is None


async def test_spent_pages_wait_for_the_next_month(w):
    w.parser.outcomes["scan.pdf"] = SCAN
    w.reader.outcomes["scan.pdf"] = read(model_cost=0.0018)
    await w.settle()   # a sync state exists
    await close_initial_build(w.repo, w.clock.now)
    await spend(w.repo, UsageClass.READER, READER_USD_PER_MONTH, w.clock.now)   # the month's reader dollars are spent
    doc = w.add_doc("scan.pdf", "")
    await w.settle()
    rec = w.rec(doc)
    november = datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert (rec.state, rec.due_at, rec.priority) == (RecordState.PENDING, november, Priority.MAINTENANCE)
    assert w.reader.requests == [] and len(w.parser.requests) == 1
    w.clock.now = november
    await w.settle()
    assert w.rec(doc).state is RecordState.INDEXED
    assert await month_usd(w.repo, UsageClass.READER, "2026-11") == pytest.approx(0.0018)   # counted in November
    assert await month_calls(w.repo, "2026-11") == 1


async def test_the_reader_reports_a_spent_budget(w):
    w.parser.outcomes["scan.pdf"] = SCAN
    w.reader.outcomes["scan.pdf"] = ExtractionOutcome(status=ExtractionStatus.BUDGET_EXHAUSTED)
    doc = w.add_doc("scan.pdf", "")
    await w.settle()
    rec = w.rec(doc)
    assert (rec.state, rec.priority, rec.due_at) == (RecordState.PENDING, Priority.MAINTENANCE,
                                                     datetime(2026, 11, 1, tzinfo=timezone.utc))


async def test_pages_left_reach_the_reader(w):
    w.parser.outcomes["scan.pdf"] = SCAN
    w.reader.outcomes["scan.pdf"] = read(pages=2)
    await w.settle()
    await close_initial_build(w.repo, w.clock.now)
    await spend(w.repo, UsageClass.READER, READER_USD_PER_MONTH - 7 * READER_PAGE_USD_ESTIMATE, w.clock.now)
    w.add_doc("scan.pdf", "")
    await w.settle()
    assert w.reader.requests[0].max_model_pages == 7


async def test_a_text_image_is_read_from_the_large_rendition(w):
    w.triage.verdicts = {}
    w.triage.default = TriageVerdict.TEXT
    w.reader.outcomes["receipt.jpg"] = read(pages=1)
    seen = []
    w.reader.before = lambda request: seen.append(open(request.path, "rb").read())
    img = w.add_image("receipt.jpg", thumbnail=PNG)
    await w.settle()
    rec = w.rec(img)
    assert (rec.state, rec.extracted_by) == (RecordState.INDEXED, "reader@1")
    assert (img, READER_RENDITION_SIZE) in w.drive.thumbnail_calls and img not in w.drive.downloads
    (request,) = w.reader.requests
    assert request.kind == "image" and seen == [PNG]


async def test_without_a_rendition_a_small_jpeg_original_is_read(w, monkeypatch):
    w.triage.default = TriageVerdict.TEXT
    w.reader.outcomes["receipt.jpg"] = read(pages=1)
    img = w.add_image("receipt.jpg")
    original = w.drive.get_thumbnail

    async def renditions(user_id, item_id, size):   # a triage thumbnail, but no large rendition
        return None if size == READER_RENDITION_SIZE else await original(user_id, item_id, size)
    monkeypatch.setattr(w.drive, "get_thumbnail", renditions)
    await w.settle()
    assert img in w.drive.downloads and w.rec(img).state is RecordState.INDEXED


async def test_without_a_rendition_heic_is_never_decoded(w, monkeypatch):
    w.triage.default = TriageVerdict.TEXT
    img = w.drive.add_file("IMG.HEIC", b"\x00\x00\x00\x18ftypheic", thumbnail=JPEG, mime_type="image/heic")
    original = w.drive.get_thumbnail

    async def renditions(user_id, item_id, size):
        return None if size == READER_RENDITION_SIZE else await original(user_id, item_id, size)
    monkeypatch.setattr(w.drive, "get_thumbnail", renditions)
    await w.settle()
    assert w.rec(img).reason is Reason.NO_TEXT and w.drive.downloads == [] and w.reader.requests == []


async def test_a_big_original_is_not_sent(w, monkeypatch):
    w.triage.default = TriageVerdict.TEXT
    img = w.drive.add_file("big.jpg", JPEG + b"\x00" * READER_IMAGE_MAX_BYTES, thumbnail=JPEG,
                           mime_type="image/jpeg")
    original = w.drive.get_thumbnail

    async def renditions(user_id, item_id, size):
        return None if size == READER_RENDITION_SIZE else await original(user_id, item_id, size)
    monkeypatch.setattr(w.drive, "get_thumbnail", renditions)
    await w.settle()
    assert w.rec(img).reason is Reason.NO_TEXT and w.drive.downloads == []


async def test_a_missing_reader_is_a_wiring_error(w):
    w.svc._extractors = {k: v for k, v in w.svc._extractors.items() if k != "reader"}
    w.parser.outcomes["scan.pdf"] = SCAN
    doc = w.add_doc("scan.pdf", "")
    result = await w.tick()
    rec = w.rec(doc)
    assert result.status == "error" and (rec.state, rec.attempts, rec.attempt_started_at) == (RecordState.PENDING, 0, None)
    assert rec.due_at == w.clock.now   # due again: the run was abandoned, not counted (§8.2)


async def test_nothing_wired_ends_the_tick_before_any_write():
    w = ContentWorld(wired=False)
    doc = w.add_doc("a.md", prose(3))
    result = await w.tick()
    assert result.status == "error"
    rec = w.rec(doc)
    assert (rec.index_generation, rec.attempts, rec.attempt_started_at) == (0, 0, None)
    state = await w.repo.get_sync_state(U_SCOPE)
    assert state.last_error.value == "internal"


async def test_a_backoff_is_not_a_continuation(w):
    w.parser.outcomes["scan.pdf"] = SCAN
    w.reader.unavailable = True
    w.add_doc("scan.pdf", "")
    await w.settle()
    before = len(w.queue.tasks)
    await w.tick()
    assert len(w.queue.tasks) == before   # deferred work waits for the fan-out, it does not chain ticks


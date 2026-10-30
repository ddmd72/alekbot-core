"""Index-run fixes from adversarial review B (USER_DRIVE_INDEXING_RFC §8.1, §8.2, §12.3, §24.5, §24.7): CPU work off
the event loop, the month budget, the chunk embedding space, and which errors are systemic."""
import threading

import pytest

from src.domain.drive_index import RecordState
from src.services import drive_indexing_service as svc_mod

from .drive_content_world import ContentWorld, admitted, prose  # noqa: F401  (fixture)
from .drive_index_fakes import LegacyConnections
from .drive_index_fakes import U_SCOPE

pytestmark = pytest.mark.usefixtures("admitted")


@pytest.fixture
def w():
    return ContentWorld()


# -- B-M1: the gate, the chunker and the terms run in a worker thread ----------------------------------------------

async def test_gate_chunker_and_terms_run_off_the_event_loop(w, monkeypatch):
    on_loop = []

    def watched(fn):
        def run(*args, **kwargs):
            on_loop.append((fn.__name__, threading.current_thread() is threading.main_thread()))
            return fn(*args, **kwargs)
        return run

    # Search v2 (§25.2.2): a chunk's terms are its word keys (`word_keys_capped`), the v1 `content_terms` retired
    for name in ("is_machine_text", "chunk_text", "word_keys_capped"):
        monkeypatch.setattr(svc_mod, name, watched(getattr(svc_mod, name)))
    doc = w.add_doc("notes.md", prose(80))
    await w.settle()
    assert w.rec(doc).state is RecordState.INDEXED
    assert {name for name, _ in on_loop} == {"is_machine_text", "chunk_text", "word_keys_capped"}
    assert not any(main for _, main in on_loop)


# -- B-M4: a file waits for the month that can read all of it ------------------------------------------------------

def _scan(pages):
    from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
    return ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=pages)


def _read(pages):
    from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
    return ExtractionOutcome(status=ExtractionStatus.TEXT, text="Receipt. Total 12.50 EUR. Thank you.",
                             pages_total=pages, pages_sent=pages, model_calls=1)


async def _pages_left(w, left):
    """Search v2 (§25.4.5): the initial build is over and the month's reader dollars pay `left` pages."""
    from src.domain.drive_enrichment import UsageClass
    from src.domain.drive_index_params import READER_PAGE_USD_ESTIMATE, READER_USD_PER_MONTH

    from .drive_usage_helpers import close_initial_build, spend
    await w.settle()   # a sync state exists
    await close_initial_build(w.repo, w.clock.now)
    await spend(w.repo, UsageClass.READER, READER_USD_PER_MONTH - left * READER_PAGE_USD_ESTIMATE, w.clock.now)


async def test_a_scan_needing_more_pages_than_are_left_waits_for_next_month(w):
    from datetime import datetime, timezone

    from src.domain.drive_index import Priority
    w.parser.outcomes["scan.pdf"] = _scan(12)
    w.reader.outcomes["scan.pdf"] = _read(12)
    await _pages_left(w, 5)
    doc = w.add_doc("scan.pdf", "")
    await w.settle()
    rec = w.rec(doc)
    november = datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert w.reader.requests == []   # never cut to the 5 pages left
    assert (rec.state, rec.due_at, rec.priority) == (RecordState.PENDING, november, Priority.MAINTENANCE)
    w.clock.now = november
    await w.settle()
    assert w.rec(doc).state is RecordState.INDEXED and w.reader.requests[0].max_model_pages >= 12


async def test_a_scan_that_fits_in_the_pages_left_is_read_now(w):
    w.parser.outcomes["scan.pdf"] = _scan(5)
    w.reader.outcomes["scan.pdf"] = _read(5)
    await _pages_left(w, 5)
    doc = w.add_doc("scan.pdf", "")
    await w.settle()
    assert w.rec(doc).state is RecordState.INDEXED and w.reader.requests[0].max_model_pages == 5


async def test_only_a_file_over_the_whole_monthly_budget_is_cut_to_it(w, monkeypatch):
    from src.domain import drive_index_run
    monkeypatch.setattr(drive_index_run, "MODEL_EXTRACT_PAGES_PER_MONTH", 8)
    monkeypatch.setattr(svc_mod, "MODEL_EXTRACT_PAGES_PER_MONTH", 8)
    w.parser.outcomes["scan.pdf"] = _scan(30)
    w.reader.outcomes["scan.pdf"] = _read(8)
    doc = w.add_doc("scan.pdf", "")
    await w.settle()
    assert w.rec(doc).state is RecordState.INDEXED and w.reader.requests[0].max_model_pages == 8


# -- minor: a deferral keeps the class the run started from ----------------------------------------------------------

async def test_an_outage_on_an_edit_keeps_it_in_the_edit_class(w):
    from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
    from src.domain.drive_index import Priority
    w.parser.outcomes["scan.pdf"] = ExtractionOutcome(status=ExtractionStatus.TEXT, text=prose(40), pages_total=1)
    doc = w.add_doc("scan.pdf", "")
    await w.settle()
    assert w.rec(doc).state is RecordState.INDEXED and w.rec(doc).has_card
    w.parser.outcomes["scan.pdf"] = _scan(3)
    w.reader.unavailable = True
    w.drive.edit(doc, b"%PDF-1.7\n", modified_at=w.clock.now)
    w.clock.advance(3600)
    await w.settle()
    rec = w.rec(doc)
    assert len(w.reader.requests) == 1 and rec.state is RecordState.PENDING
    assert (rec.priority, rec.defer_priority) == (int(Priority.EDIT), int(Priority.EDIT))


# -- minor: no rendition is retried before it is a photograph -----------------------------------------------------------

async def test_a_missing_rendition_is_retried_before_it_is_a_photograph(w):
    from src.domain.drive_index import Reason, TriageVerdict
    from src.domain.drive_index_params import MAX_ATTEMPTS, RETRY_CAP
    img = w.add_image("odd.jpg", thumbnail=None)
    await w.settle()
    rec = w.rec(img)
    assert (rec.state, rec.attempts, rec.triage_verdict) == (RecordState.PENDING, 1, None)
    w.drive.thumbnails[img] = b"\xff\xd8\xff\xe0" + b"\x00" * 64   # the provider made it meanwhile
    w.clock.advance(int(RETRY_CAP.total_seconds()))
    await w.settle()
    assert w.rec(img).triage_verdict is not None and len(w.triage.calls) == 1
    other = w.add_image("never.jpg", thumbnail=None)
    for _ in range(MAX_ATTEMPTS):
        await w.settle()
        w.clock.advance(int(RETRY_CAP.total_seconds()))
    rec = w.rec(other)
    assert (rec.reason, rec.caption, rec.triage_verdict) == (Reason.PHOTOGRAPH, "", TriageVerdict.PHOTO)


# -- B-M7: which errors end the tick ------------------------------------------------------------------------------------

class _Failing:
    """Embeddings that fail on chunk texts holding `marker` (cards embed as usual)."""

    def __init__(self, inner, marker, exc):
        self.inner, self.marker, self.exc = inner, marker, exc

    async def get_embedding(self, text, task_type="RETRIEVAL_DOCUMENT"):
        if self.marker in text and " | " not in text:
            raise self.exc
        return await self.inner.get_embedding(text, task_type)


async def test_an_unknown_error_in_a_run_is_a_counted_per_file_retry(w):
    w.svc._embeddings = _Failing(w.emb, "poison", KeyError("unexpected"))
    bad = w.add_doc("bad.md", prose(20, "poison"))
    good = w.add_doc("good.md", prose(20))
    result = await w.tick()
    rec = w.rec(bad)
    state = await w.repo.get_sync_state(U_SCOPE)
    assert result.status == "done" and state.systemic_failures == 0
    assert (rec.state, rec.attempts, rec.attempt_started_at) == (RecordState.PENDING, 1, None)
    assert rec.defer_until is not None
    await w.settle()
    assert w.rec(good).state is RecordState.INDEXED


async def test_a_known_transport_error_in_a_run_stays_systemic(w):
    from src.domain.exceptions import LLMUnavailableError
    w.svc._embeddings = _Failing(w.emb, "poison", LLMUnavailableError("embeddings down"))
    bad = w.add_doc("bad.md", prose(20, "poison"))
    result = await w.tick()
    rec = w.rec(bad)
    assert result.status == "throttled"
    assert (rec.state, rec.attempts, rec.attempt_started_at) == (RecordState.PENDING, 0, None)


async def test_a_wiring_error_in_a_run_stays_systemic(w):
    w.parser.outcomes["scan.pdf"] = _scan(3)
    w.svc._extractors = {k: v for k, v in w.svc._extractors.items() if k != "reader"}
    doc = w.add_doc("scan.pdf", "")
    result = await w.tick()
    assert result.status == "error" and w.rec(doc).attempts == 0


async def test_an_error_after_the_outcome_is_committed_never_rolls_it_back(w, monkeypatch):
    from src.domain.exceptions import LLMUnavailableError
    doc = w.add_doc("notes.md", prose(40))
    real = w.repo.delete_chunks_except
    failed = []

    async def flaky(user_id, item_id, keep):
        if not failed:
            failed.append(item_id)
            raise LLMUnavailableError("blip after the commit")
        return await real(user_id, item_id, keep)

    monkeypatch.setattr(w.repo, "delete_chunks_except", flaky)
    await w.tick()
    assert failed == [doc]
    rec = w.rec(doc)
    assert rec.state is RecordState.INDEXED and rec.indexed_generation == 1 and rec.attempt_started_at is None


async def test_an_unknown_error_after_the_commit_keeps_the_outcome_and_the_tick(w, monkeypatch):
    doc = w.add_doc("notes.md", prose(40))
    real = w.repo.delete_chunks_except
    failed = []

    async def flaky(user_id, item_id, keep):
        if not failed:
            failed.append(item_id)
            raise RuntimeError("cleanup bug")
        return await real(user_id, item_id, keep)

    monkeypatch.setattr(w.repo, "delete_chunks_except", flaky)
    result = await w.tick()
    rec = w.rec(doc)
    assert failed == [doc] and result.status == "done"
    assert rec.state is RecordState.INDEXED and rec.attempts == 0


# -- B-M6: a new embedding space re-embeds the stored chunks --------------------------------------------------------

def _switch_space(w, space):
    w.svc = svc_mod.DriveIndexingService(w.drive, w.repo, w.emb, w.queue, w.users, lambda lat, lon: None, space,
                                         extractors=w.svc._extractors, triage=w.svc._triage, connections=LegacyConnections(w.users, w.drive), clock=w.clock,
                                         monotonic=w.clock.monotonic)


async def test_a_new_space_re_embeds_chunks_from_their_stored_text(w):
    doc = w.add_doc("notes.md", prose(150))
    await w.settle()
    before = w.rec(doc)
    texts = [c.text for c in w.readable(doc)]
    assert before.chunk_space == "test-space/8" and len(texts) > 1
    downloads, parses = len(w.drive.downloads), len(w.parser.requests)
    _switch_space(w, "new-space/768")
    w.emb.calls.clear()
    await w.settle()
    rec = w.rec(doc)
    state = await w.repo.get_sync_state(U_SCOPE)
    assert (len(w.drive.downloads), len(w.parser.requests)) == (downloads, parses)   # no download, no extraction
    assert rec.chunk_space == "new-space/768" and not rec.chunk_reembed_pending
    assert rec.indexed_generation == before.indexed_generation + 1 and rec.state is RecordState.INDEXED
    assert [c.text for c in w.readable(doc)] == texts and w.generations(doc) == [rec.indexed_generation]
    assert all(t in w.emb.calls for t in texts)
    assert state.applied_space == "new-space/768"


async def test_the_space_is_not_applied_while_chunks_are_in_the_old_one(w):
    doc = w.add_doc("notes.md", prose(150))
    await w.settle()
    _switch_space(w, "new-space/768")
    await w.tick()   # the upgrade pass schedules the chunks; the search keeps their stream off meanwhile
    state = await w.repo.get_sync_state(U_SCOPE)
    assert w.rec(doc).chunk_reembed_pending and w.rec(doc).chunk_space == "test-space/8"
    assert not state.cards_in_space("new-space/768")
    await w.settle()
    state = await w.repo.get_sync_state(U_SCOPE)
    assert not w.rec(doc).chunk_reembed_pending and state.cards_in_space("new-space/768")

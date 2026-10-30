"""A new embedding space re-embeds readable chunks whatever the record's content state (USER_DRIVE_INDEXING_RFC §12.3,
§7.3, §24.7; round-2 review T-M1). A failed record keeps its previous readable generation (§3.4), and a pending one
keeps it until its new run commits (§7.4): their chunks are re-embedded from the stored text like an indexed
record's, with the content state, attempts and deferral untouched, so the space change completes and the quiet period
and the month hold still stand."""
from datetime import datetime, timezone

import pytest

from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import RecordState
from src.domain.drive_enrichment import UsageClass
from src.domain.drive_index_params import (
    MAX_ATTEMPTS, QUIET, READER_PAGE_USD_ESTIMATE, READER_USD_PER_MONTH, RETRY_CAP,
)
from src.services import drive_indexing_service as svc_mod

from .drive_content_world import ContentWorld, admitted, prose  # noqa: F401  (fixture)
from .drive_usage_helpers import close_initial_build, spend
from .drive_index_fakes import LegacyConnections
from .drive_index_fakes import U_SCOPE

pytestmark = pytest.mark.usefixtures("admitted")

NEW = "new-space/768"


def _world():
    """Each monotonic read moves the clock a little, so a tick's budgets run out as in production: a record that is
    due again at once ends the tick instead of looping inside it."""
    w = ContentWorld()

    def ticking():
        w.clock.mono += 0.05
        return w.clock.mono

    w.clock.monotonic = ticking
    w.svc._monotonic = ticking
    return w


def _switch_space(w, space):
    w.svc = svc_mod.DriveIndexingService(w.drive, w.repo, w.emb, w.queue, w.users, lambda lat, lon: None, space,
                                         extractors=w.svc._extractors, triage=w.svc._triage, connections=LegacyConnections(w.users, w.drive), clock=w.clock,
                                         monotonic=w.clock.monotonic)


async def _assert_switched(w, doc, texts):
    rec = w.rec(doc)
    state = await w.repo.get_sync_state(U_SCOPE)
    assert rec.chunk_space == NEW and not rec.chunk_reembed_pending
    assert [c.text for c in w.readable(doc)] == texts and w.generations(doc) == [rec.indexed_generation]
    assert state.applied_space == NEW and state.cards_in_space(NEW)


async def test_a_failed_record_re_embeds_its_previous_generation():
    w = _world()
    doc = w.add_doc("notes.md", prose(150))
    await w.settle()
    texts = [c.text for c in w.readable(doc)]
    w.parser.outcomes["notes.md"] = ExtractionOutcome(status=ExtractionStatus.ERROR, detail="boom")
    w.drive.edit(doc, prose(160).encode())
    for _ in range(MAX_ATTEMPTS + 2):
        w.clock.advance(RETRY_CAP.total_seconds())
        await w.settle()
    failed = w.rec(doc)
    assert failed.state is RecordState.FAILED and failed.indexed_generation is not None
    parses = len(w.parser.requests)
    _switch_space(w, NEW)
    await w.settle(max_ticks=20)
    rec = w.rec(doc)
    await _assert_switched(w, doc, texts)
    assert (rec.state, rec.reason, rec.attempts) == (RecordState.FAILED, failed.reason, failed.attempts)
    assert len(w.parser.requests) == parses and rec.due_at is None


def _scan(pages):
    return ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=pages)


async def test_a_month_held_record_re_embeds_and_stays_held():
    w = _world()
    w.parser.outcomes["scan.pdf"] = ExtractionOutcome(status=ExtractionStatus.TEXT, text=prose(150), pages_total=1)
    doc = w.add_doc("scan.pdf", "")
    await w.settle()
    texts = [c.text for c in w.readable(doc)]
    # Search v2 (§25.4.5): the initial build is over and the month's reader dollars pay 5 pages
    await close_initial_build(w.repo, w.clock.now)
    await spend(w.repo, UsageClass.READER, READER_USD_PER_MONTH - 5 * READER_PAGE_USD_ESTIMATE, w.clock.now)
    w.parser.outcomes["scan.pdf"] = _scan(12)
    w.drive.edit(doc, b"%PDF-1.7\n")
    w.clock.advance(QUIET.total_seconds() + 60)
    await w.settle()
    november = datetime(2026, 11, 1, tzinfo=timezone.utc)
    held = w.rec(doc)
    assert held.state is RecordState.PENDING and held.defer_until == november and w.reader.requests == []
    _switch_space(w, NEW)
    await w.settle(max_ticks=20)
    rec = w.rec(doc)
    await _assert_switched(w, doc, texts)
    assert (rec.state, rec.defer_until, rec.defer_priority, rec.attempts) == (
        RecordState.PENDING, november, held.defer_priority, held.attempts)
    assert rec.due_at == november and w.reader.requests == []


async def test_a_record_in_its_quiet_period_re_embeds_and_keeps_the_quiet_period():
    w = _world()
    doc = w.add_doc("notes.md", prose(150))
    await w.settle()
    texts = [c.text for c in w.readable(doc)]
    edited_at = w.clock.now
    w.drive.edit(doc, prose(160).encode(), modified_at=edited_at)
    await w.tick()   # observed: dirty, its content waits for QUIET
    quiet = w.rec(doc)
    assert quiet.state is RecordState.PENDING and quiet.dirty_since is not None
    parses = len(w.parser.requests)
    _switch_space(w, NEW)
    await w.settle(max_ticks=20)
    rec = w.rec(doc)
    assert w.clock.now < edited_at + QUIET
    await _assert_switched(w, doc, texts)
    assert len(w.parser.requests) == parses   # the edit was not read inside its quiet period
    assert rec.state is RecordState.PENDING and rec.due_at == edited_at + QUIET
    w.clock.now = edited_at + QUIET
    await w.settle()
    rec = w.rec(doc)
    assert rec.state is RecordState.INDEXED and rec.chunk_space == NEW and len(w.parser.requests) == parses + 1

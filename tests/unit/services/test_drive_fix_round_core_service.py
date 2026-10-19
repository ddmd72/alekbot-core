"""Cross-cutting fix round, service parts (USER_DRIVE_INDEXING_RFC §26; A4, B-minor 2 and 3).

A4: a scan waiting for the reader keeps the capture the parser's signals decided (never a model word).
B-minor 2: files the bot cannot open are a sentence of their own, counted in the search's scope, never a reason for
"Coverage: incomplete".
B-minor 3: a file that enters metadata_only without a read loses its capture."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.domain.drive_capture import CaptureSignals
from src.domain.drive_enrichment import UsageClass
from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import RecordState
from src.domain.drive_index_params import READER_USD_PER_MONTH
from src.domain.drive_search import INACCESSIBLE_COUNTER, render_results

from .drive_content_world import ContentWorld, admitted, prose  # noqa: F401  (fixture)
from .drive_usage_helpers import close_initial_build, spend
from .test_drive_search_service import World

SCANNER = CaptureSignals(pages=2, text_pages=0, raster_pages=2, median_dpi=200.0, page_short_mm=140.0,
                         page_long_mm=190.0)
MIXED = CaptureSignals(pages=2, text_pages=1, raster_pages=1, median_dpi=200.0, page_short_mm=210.0,
                       page_long_mm=297.0)
SINCE = datetime(2026, 10, 11, 12, 30, tzinfo=timezone.utc)


def _no_text(signals):
    return ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=2, capture_signals=signals)


@pytest.mark.usefixtures("admitted")
class TestCaptureOnAHold:     # A4
    async def _spent_world(self, signals):
        w = ContentWorld()
        w.parser.outcomes["scan.pdf"] = _no_text(signals)
        await w.settle()
        await close_initial_build(w.repo, w.clock.now)
        await spend(w.repo, UsageClass.READER, READER_USD_PER_MONTH, w.clock.now)
        return w

    async def test_a_scan_waiting_for_the_monthly_cap_is_still_a_detected_scan(self):
        w = await self._spent_world(SCANNER)
        doc = w.add_doc("scan.pdf", "")
        await w.settle()
        rec = w.rec(doc)
        assert rec.state is RecordState.PENDING and w.reader.requests == []
        assert (rec.capture, rec.capture_source) == ("scan", "detected")

    async def test_the_reader_reporting_a_spent_budget_keeps_it_too(self):
        w = ContentWorld()
        w.parser.outcomes["scan.pdf"] = _no_text(SCANNER)
        w.reader.outcomes["scan.pdf"] = ExtractionOutcome(status=ExtractionStatus.BUDGET_EXHAUSTED)
        doc = w.add_doc("scan.pdf", "")
        await w.settle()
        rec = w.rec(doc)
        assert rec.state is RecordState.PENDING and (rec.capture, rec.capture_source) == ("scan", "detected")

    async def test_an_ambiguous_pdf_on_a_hold_has_no_capture(self):
        w = await self._spent_world(MIXED)
        doc = w.add_doc("scan.pdf", "")
        await w.settle()
        rec = w.rec(doc)
        assert rec.state is RecordState.PENDING and (rec.capture, rec.capture_source) == ("", "")


@pytest.mark.usefixtures("admitted")
async def test_a_file_entering_metadata_only_without_a_read_loses_its_capture():     # B-minor 3
    w = ContentWorld()
    w.parser.outcomes["a.pdf"] = ExtractionOutcome(status=ExtractionStatus.TEXT, text=prose(30, "lease"), pages_total=2,
                                                   capture_signals=SCANNER)
    doc = w.add_doc("a.pdf", "")
    await w.settle()
    assert w.rec(doc).state is RecordState.INDEXED and w.rec(doc).capture == "scan"
    w.drive.rename(doc, "a.bin")
    w.clock.advance(60)
    await w.settle()
    rec = w.rec(doc)
    assert rec.state is RecordState.METADATA_ONLY and rec.indexed_generation is None
    assert (rec.capture, rec.capture_source) == ("", "")


class TestClosedFilesInCoverage:     # B-minor 2
    @pytest.fixture
    async def w(self):
        world = World()
        world.shared = world.drive.add("Shared", folder=True)
        world.mine = world.drive.add("Mine", folder=True)
        world.closed = world.drive.add("residence card.pdf", parent=world.shared)
        world.open_one = world.drive.add("lease agreement.pdf", parent=world.mine)
        world.open_two = world.drive.add("lease agreement.pdf", parent=world.shared)
        await world.settle()
        world.set_state(world.closed, inaccessible_since=SINCE, inaccessible_checked_at=SINCE)
        state = world.repo.states["u"]
        world.repo.states["u"] = replace(state, counters={**state.counters, INACCESSIBLE_COUNTER: 1})
        return world

    async def test_they_are_their_own_sentence_not_an_incomplete_coverage(self, w):
        text = render_results(await w.search("lease agreement"))
        assert "Coverage: complete." in text and "Coverage: incomplete" not in text
        assert "1 file cannot be opened by the bot" in text

    async def test_a_folder_search_counts_only_the_closed_files_inside_it(self, w):
        inside = await w.search("lease agreement", folder="Shared")
        outside = await w.search("lease agreement", folder="Mine")
        assert inside.tiered.coverage.inaccessible == 1
        assert outside.tiered.coverage.inaccessible == 0
        assert "cannot be opened by the bot" not in render_results(outside)

    async def test_a_real_gap_still_makes_coverage_incomplete(self, w):
        result = await w.search("lease agreement")
        result = replace(result, tiered=replace(result.tiered, coverage=replace(result.tiered.coverage, not_read_yet=2)))
        text = render_results(result)
        assert "Coverage: incomplete" in text and "2 documents are not read yet" in text
        assert "1 file cannot be opened by the bot" in text and "cannot be opened by the bot." in text

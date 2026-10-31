"""Records stale only because their re-read waits for next month's budget are parked by the upgrade pass (R3 F1-5,
USER_DRIVE_INDEXING_RFC §12.3, §24.7), like records parked under an unresolved parent: a working tick does not restart
a full pass over the index for them (a page read of every record plus a parent read per stale record). The pass runs
again on a target change or when the month turns."""
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.drive_enrichment import UsageClass
from src.domain.drive_extraction import PARSER, READER, ExtractionOutcome, ExtractionStatus
from src.domain.drive_index_params import QUIET, READER_USD_PER_MONTH
from src.services import drive_indexing_service as svc_mod

from .drive_content_world import ContentWorld, HookedExtractor, admitted, prose  # noqa: F401  (fixture)
from .drive_usage_helpers import close_initial_build, spend
from .drive_index_fakes import LegacyConnections
from .drive_index_fakes import U_SCOPE

pytestmark = pytest.mark.usefixtures("admitted")


def rewire(w, reader):
    w.reader = reader
    w.svc = svc_mod.DriveIndexingService(w.drive, w.repo, w.emb, w.queue, w.users, lambda lat, lon: "",
                                         "test-space/8", extractors={PARSER: w.parser, READER: reader},
                                         triage=w.triage, connections=LegacyConnections(w.users, w.drive), clock=w.clock, monotonic=w.clock.monotonic)


def scan_outcomes(w, reader, name):
    w.parser.outcomes[name] = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=1)
    reader.outcomes[name] = ExtractionOutcome(status=ExtractionStatus.TEXT, text=prose(5), pages_total=1,
                                              pages_sent=1, model_calls=1)


async def test_month_held_records_do_not_restart_the_upgrade_pass_on_every_working_tick(monkeypatch):
    w = ContentWorld(page_size=500)
    scans = []
    for i in range(3):
        scan_outcomes(w, w.reader, f"scan{i}.pdf")
        scans.append(w.add_doc(f"scan{i}.pdf", ""))
    notes = w.add_doc("notes.md", prose(20))
    await w.settle()
    assert all(w.rec(s).extracted_by == "reader@1" for s in scans)
    # this month's reader dollars are spent (Search v2, §25.4.5: the initial build over, the month's cap used up)
    await close_initial_build(w.repo, w.clock.now)
    await spend(w.repo, UsageClass.READER, READER_USD_PER_MONTH, w.clock.now)
    new_reader = HookedExtractor(READER, version=2)
    for i in range(3):
        scan_outcomes(w, new_reader, f"scan{i}.pdf")
    rewire(w, new_reader)
    await w.settle()   # the pass schedules the re-reads; the month holds them
    month_start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert all(w.rec(s).defer_until == month_start for s in scans) and new_reader.requests == []

    pages = []
    original = w.repo.records_page

    async def counting(user_id, after, limit):
        pages.append(after)
        return await original(user_id, after, limit)

    w.repo.records_page = counting
    for _ in range(4):   # working ticks: the user edits a note
        w.drive.edit(notes, prose(21).encode(), modified_at=w.clock.now)
        w.clock.advance(QUIET.total_seconds() + 1)
        await w.settle()
    assert pages == []   # no new pass for records the month holds

    w.clock.now = month_start + timedelta(minutes=1)   # the month turns: the held re-reads run, the pass verifies
    await w.settle()
    assert pages != []
    assert {w.rec(s).extracted_by for s in scans} == {"reader@2"}
    state = await w.repo.get_sync_state(U_SCOPE)
    assert state.applied_versions["extractor:reader"] == 2

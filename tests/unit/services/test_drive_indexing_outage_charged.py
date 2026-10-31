"""A file that always ends the reader in an "outage" cannot retry forever (final review F1-1; USER_DRIVE_INDEXING_RFC
§24.3, §24.8): an outage raised while the reader was reading this file (it made at least one model call for it) is
charged to the file. The file waits for the model backoff in class 3, behind other due work; the MAX_ATTEMPTS-th
charged outage is a counted per-file failure, `failed`. An outage before any call (a missing prompt, a provider the
indexer cannot build) is the model's alone and charges nothing; a new version or a finished read starts the count
again."""
from datetime import timedelta

import pytest

from src.domain.drive_extraction import DriveModelUnavailable, ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import Priority, Reason, RecordState
from src.domain.drive_index_params import MAX_ATTEMPTS

from src.domain.drive_enrichment import UsageClass
from src.domain.drive_index_planner import month_key

from .drive_content_world import ContentWorld, admitted  # noqa: F401  (fixture)
from .drive_usage_helpers import month_usd
from .drive_index_fakes import U_SCOPE

pytestmark = pytest.mark.usefixtures("admitted")

SCAN = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=30)
READ = ExtractionOutcome(status=ExtractionStatus.TEXT, text="Invoice total 12.50 EUR, paid in full.", pages_total=30,
                         pages_sent=30, model_calls=3)
ROUND = 4 * 3600   # past any model backoff


@pytest.fixture
def w():
    w = ContentWorld()
    for name in ("poison.pdf", "good.pdf"):
        w.parser.outcomes[name] = SCAN
        w.reader.outcomes[name] = READ
    return w


def _poison(w, **usage):
    usage = usage or {"pages_sent": 20, "model_calls": 3, "model_cost": 0.01}

    def before(request):
        if request.name == "poison.pdf":
            w.poison_reads = getattr(w, "poison_reads", 0) + 1
            raise DriveModelUnavailable("HTTP 503 on window 3", **usage)
    w.reader.before = before


def _reads_of(w, name):
    return getattr(w, "poison_reads", 0)


async def test_charged_outages_end_in_failed():
    w = ContentWorld()
    w.parser.outcomes["poison.pdf"] = SCAN
    _poison(w)
    doc = w.add_doc("poison.pdf", "")
    await w.settle()
    rec = w.rec(doc)
    state = await w.repo.get_sync_state(U_SCOPE)
    assert (rec.state, rec.model_outages, rec.attempts) == (RecordState.PENDING, 1, 0)
    assert (rec.priority, rec.due_at) == (int(Priority.MAINTENANCE), state.model_backoff_until)
    for _ in range(MAX_ATTEMPTS + 3):
        w.clock.advance(ROUND)
        await w.settle()
    rec = w.rec(doc)
    assert (rec.state, rec.reason) == (RecordState.FAILED, Reason.EXTRACT_ERROR)
    assert rec.extracted_by == "reader@1"   # a new reader version gives it a fresh try
    assert _reads_of(w, "poison.pdf") == MAX_ATTEMPTS
    # bounded: each charged try counted, then no more — v2 (§25.4.5) counts the reader's dollars, and the pages
    # each try sent still reach the tick line
    assert await month_usd(w.repo, UsageClass.READER, month_key(w.clock.now)) == pytest.approx(0.01 * MAX_ATTEMPTS)
    assert w.summary_total("model_pages") == 20 * MAX_ATTEMPTS


async def test_a_charged_file_waits_behind_other_due_work(w):
    _poison(w)
    poison = w.add_doc("poison.pdf", "")
    await w.settle()
    good = w.add_doc("good.pdf", "")
    await w.settle()   # the model is in backoff: the new file waits for it too, in its own class
    w.clock.advance(ROUND)
    before = len(w.reader.requests)
    await w.settle()
    after = [r.name for r in w.reader.requests[before:]]
    assert after[0] == "good.pdf" and w.rec(good).state is RecordState.INDEXED
    assert w.rec(poison).model_outages == 2


async def test_an_outage_before_any_model_call_charges_nothing(w):
    w.reader.unavailable = True   # raised before any call: no model_calls
    doc = w.add_doc("good.pdf", "")
    for _ in range(MAX_ATTEMPTS + 2):
        await w.settle()
        w.clock.advance(ROUND)
    rec = w.rec(doc)
    assert (rec.state, rec.model_outages, rec.attempts) == (RecordState.PENDING, 0, 0)
    assert rec.priority == int(Priority.FIRST_SEEN)
    w.reader.unavailable = False
    await w.settle()
    assert w.rec(doc).state is RecordState.INDEXED


async def test_a_finished_read_ends_the_count(w):
    _poison(w)
    doc = w.add_doc("poison.pdf", "")
    await w.settle()
    w.clock.advance(ROUND)
    await w.settle()
    assert w.rec(doc).model_outages == 2
    w.reader.before = None
    w.clock.advance(ROUND)
    await w.settle()
    rec = w.rec(doc)
    assert (rec.state, rec.model_outages) == (RecordState.INDEXED, 0)


async def test_a_new_version_ends_the_count(w):
    _poison(w)
    doc = w.add_doc("poison.pdf", "")
    await w.settle()
    w.clock.advance(ROUND)
    await w.settle()
    assert w.rec(doc).model_outages == 2
    w.drive.edit(doc, b"%PDF-1.7\nnew", modified_at=w.clock.now - timedelta(hours=1))
    await w.settle()
    rec = w.rec(doc)
    assert rec.state is RecordState.PENDING and rec.model_outages <= 1   # 0, or 1 if the new version was tried
    w.clock.advance(ROUND)
    await w.settle()
    assert w.rec(doc).state is RecordState.PENDING   # three charges are needed again before it fails

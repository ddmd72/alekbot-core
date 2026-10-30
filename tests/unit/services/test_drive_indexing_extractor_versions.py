"""Per-extractor re-extraction (USER_DRIVE_INDEXING_RFC §9, §12.3): a new version of a link, or a link that leaves a
kind's chain, re-extracts exactly what that link decided, in class 3, with the old chunks serving meanwhile."""
import pytest

from src.domain import drive_extraction
from src.domain.drive_extraction import PARSER, READER, ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import CURRENT_VERSIONS, Priority, Reason, RecordState, TriageVerdict
from src.domain.drive_index_params import MAX_ATTEMPTS, RETRY_CAP
from src.services.drive_indexing_service import DriveIndexingService

from .drive_content_world import ContentWorld, HookedExtractor, admitted, prose  # noqa: F401  (fixture)
from .drive_index_fakes import LegacyConnections
from .drive_index_fakes import U_SCOPE

pytestmark = pytest.mark.usefixtures("admitted")


def rewire(w, **extractors):
    """The indexer redeployed with other extractors over the same index."""
    links = {PARSER: w.parser, READER: w.reader, **extractors}
    w.svc = DriveIndexingService(w.drive, w.repo, w.emb, w.queue, w.users, lambda lat, lon: "", "test-space/8",
                                 extractors=links, triage=w.triage, connections=LegacyConnections(w.users, w.drive), clock=w.clock, monotonic=w.clock.monotonic)


@pytest.fixture
async def w():
    world = ContentWorld()
    world.md = world.add_doc("notes.md", prose(150))
    world.pdf = world.add_doc("contract.pdf", prose(150))
    world.parser.outcomes["scan.pdf"] = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=2)
    world.reader.outcomes["scan.pdf"] = ExtractionOutcome(status=ExtractionStatus.TEXT, text=prose(5), pages_total=2,
                                                          pages_sent=2, model_calls=1)
    world.scan = world.add_doc("scan.pdf", "")
    world.triage.default = TriageVerdict.TEXT
    world.reader.outcomes["receipt.jpg"] = ExtractionOutcome(status=ExtractionStatus.TEXT, text="Total 12 EUR",
                                                             pages_total=1, pages_sent=1, model_calls=1)
    world.img = world.add_image("receipt.jpg")
    await world.settle()
    state = await world.repo.get_sync_state(U_SCOPE)
    assert dict(state.applied_versions) == {**CURRENT_VERSIONS, "extractor:parser": 1, "extractor:reader": 1}
    return world


def runs(extractor, name):
    return sum(1 for r in extractor.requests if r.name == name)


async def test_a_new_parser_version_re_extracts_what_the_parser_read(w):
    new_parser = HookedExtractor(PARSER, version=2)
    new_parser.outcomes = dict(w.parser.outcomes)
    rewire(w, parser=new_parser)
    await w.tick()   # the pass schedules: fresh work first
    for item in (w.md, w.pdf):
        rec = w.rec(item)
        assert rec.state is RecordState.PENDING and rec.priority == Priority.MAINTENANCE
        assert w.readable(item)   # the old generation serves until the new one commits (§7.4)
    assert w.rec(w.scan).state is RecordState.INDEXED and w.rec(w.img).state is RecordState.INDEXED
    await w.settle()
    assert w.rec(w.md).extracted_by == "parser@2" and w.rec(w.pdf).extracted_by == "parser@2"
    assert w.rec(w.md).indexed_generation == 2 and w.generations(w.md) == [2]
    assert runs(new_parser, "scan.pdf") == 0 and len(w.reader.requests) == 2   # the reader's work stands
    state = await w.repo.get_sync_state(U_SCOPE)
    assert dict(state.applied_versions)["extractor:parser"] == 2


async def test_a_new_reader_version_re_reads_scans_and_images_only(w):
    new_reader = HookedExtractor(READER, version=2)
    new_reader.outcomes = dict(w.reader.outcomes)
    rewire(w, reader=new_reader)
    await w.settle()
    assert {r.name for r in new_reader.requests} == {"scan.pdf", "receipt.jpg"}
    assert w.rec(w.scan).extracted_by == "reader@2" and w.rec(w.img).extracted_by == "reader@2"
    assert runs(w.parser, "notes.md") == 1
    assert len(w.triage.calls) == 1   # a verdict does not depend on the reader


async def test_a_new_link_for_one_kind_re_extracts_that_kind_only(w, monkeypatch):
    pdf_parser = HookedExtractor("pdf_parser", version=1)
    monkeypatch.setattr(drive_extraction, "EXTRACTOR_CHAINS", {"pdf": ("pdf_parser", READER), "image": (READER,)})
    rewire(w, pdf_parser=pdf_parser)
    pdf_parser.outcomes["scan.pdf"] = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=2)
    await w.settle()
    assert {r.name for r in pdf_parser.requests} == {"contract.pdf"}   # the scan's text is the reader's: it stands
    assert w.rec(w.pdf).extracted_by == "pdf_parser@1" and w.rec(w.md).extracted_by == "parser@1"
    assert runs(w.parser, "notes.md") == 1


async def test_a_failure_under_the_new_version_ends_the_pass(w):
    new_parser = HookedExtractor(PARSER, version=2)
    new_parser.outcomes = {**w.parser.outcomes, "notes.md": ExtractionOutcome(status=ExtractionStatus.ERROR)}
    rewire(w, parser=new_parser)
    for _ in range(MAX_ATTEMPTS + 2):
        await w.settle()
        w.clock.advance(RETRY_CAP.total_seconds())
    rec = w.rec(w.md)
    assert (rec.state, rec.reason, rec.extracted_by) == (RecordState.FAILED, Reason.EXTRACT_ERROR, "parser@2")
    assert w.readable(w.md)   # failed keeps the old chunks (§3.4)
    state = await w.repo.get_sync_state(U_SCOPE)
    assert dict(state.applied_versions)["extractor:parser"] == 2 and not state.upgrade_found_stale


async def test_a_record_without_an_extraction_is_never_extractor_stale(w):
    code = w.drive.add_file("main.py", b"print(1)")
    await w.settle()
    rewire(w, parser=HookedExtractor(PARSER, version=2))
    await w.settle()
    assert w.rec(code).due_at is None and w.rec(code).extracted_by is None

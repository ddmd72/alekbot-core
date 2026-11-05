"""U5 Part D: an upgrade re-read whose text gives the stored chunks commits as a stamp (Layer 2); an upgrade re-read
never calls the paid reader (O-3 = (b)); an edit still does."""
from dataclasses import replace

import pytest

from src.domain.document_extraction import PARSER, READER, ExtractionStatus
from src.domain.drive_extraction import ExtractionOutcome
from src.domain.drive_index import Reason, RecordState
from src.services.drive_indexing_service import DriveIndexingService

from .drive_content_world import ContentWorld, HookedExtractor, admitted, prose  # noqa: F401  (fixture)
from .drive_inaccessible_world import ClosedWorld
from .drive_index_fakes import LegacyConnections

pytestmark = pytest.mark.usefixtures("admitted")


def upgraded(w, **outcomes):
    """The indexer redeployed with parser@2 (scripted `outcomes`) over the same index."""
    parser = HookedExtractor(PARSER, version=2)
    parser.outcomes = dict(outcomes)
    w.parser = parser
    w.svc = DriveIndexingService(w.drive, w.repo, w.emb, w.queue, w.users, lambda lat, lon: "", "test-space/8",
                                 extractors={PARSER: parser, READER: w.reader}, triage=w.triage,
                                 connections=LegacyConnections(w.users, w.drive), clock=w.clock,
                                 monotonic=w.clock.monotonic)
    return parser


def thin(pages=3):
    """A parser outcome the router sends to the reader (under MODEL_EXTRACT_DENSITY chars per page)."""
    return ExtractionOutcome(status=ExtractionStatus.TEXT, text="Lab report LAB-2025-03-0417", pages_total=pages)


def garbage():
    """Text over MODEL_EXTRACT_GARBAGE unreadable characters, dense enough not to be low_density."""
    return ExtractionOutcome(status=ExtractionStatus.TEXT, text=("(cid:12)" * 40 + " Lab report LAB-2025-03-0417 ") * 3,
                             pages_total=1)


ROUTED = {"no_text": ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=2),
          "low_density": thin(), "garbage_text": garbage()}


@pytest.fixture
async def w():
    world = ContentWorld()
    world.doc = world.add_doc("ipo-report.md", prose(150))
    world.pdf = world.add_doc("lab-report.pdf", "")
    world.parser.outcomes["lab-report.pdf"] = ExtractionOutcome(status=ExtractionStatus.TEXT, text=prose(30),
                                                                pages_total=1)
    await world.settle()
    return world


async def test_an_unchanged_text_commits_as_a_stamp(w):
    before, chunks, embeds = w.rec(w.doc), w.chunks(w.doc), len(w.emb.calls)
    upgraded(w, **{"lab-report.pdf": w.parser.outcomes["lab-report.pdf"]})
    await w.settle()
    rec = w.rec(w.doc)
    assert rec.extracted_by == "parser@2" and rec.state is RecordState.INDEXED
    assert rec.indexed_generation == before.indexed_generation and w.chunks(w.doc) == chunks
    assert rec.index_generation > before.index_generation   # the run's generation number is burned
    assert len(w.emb.calls) == embeds
    assert w.summary_total("reread_same") == 2   # the document and the PDF


async def test_one_changed_chunk_embeds_exactly_that_text_into_a_new_generation(w):
    old = {c.text for c in w.chunks(w.doc)}
    changed = prose(150).replace("clause number 149 sets", "clause number 149 (washer warranty WX800-55-1209) sets")
    parser = upgraded(w, **{"lab-report.pdf": w.parser.outcomes["lab-report.pdf"]})
    parser.outcomes["ipo-report.md"] = ExtractionOutcome(status=ExtractionStatus.TEXT, text=changed)
    embeds = len(w.emb.calls)
    await w.settle()
    rec = w.rec(w.doc)
    new = {c.text for c in w.readable(w.doc)}
    assert rec.extracted_by == "parser@2" and rec.indexed_generation == 2 and w.generations(w.doc) == [2]
    embedded = [t for t in w.emb.calls[embeds:] if t in new]   # chunk texts (the card's text is not a chunk)
    assert 0 < len(embedded) < len(new) and not set(embedded) & old
    assert w.summary_total("vectors_reused") >= 1


async def test_an_edit_during_the_run_drops_the_result(w):
    parser = upgraded(w)

    def edit(request):   # the record's content version moves between the read and the fence
        if request.name == "ipo-report.md":
            w.repo.records[("u", w.doc)] = replace(w.rec(w.doc), content_version="edited-during-run")
    parser.before = edit
    await w.tick()
    await w.tick()
    assert w.summary_total("dropped") == 1
    assert w.rec(w.doc).extracted_by == "parser@1"   # nothing of the dropped run was written


@pytest.mark.parametrize("why", sorted(ROUTED))
async def test_an_upgrade_re_read_never_calls_the_reader(w, why):
    outcome = ROUTED[why]
    calls = len(w.reader.requests)
    upgraded(w, **{"lab-report.pdf": outcome})
    await w.settle()
    assert len(w.reader.requests) == calls == 0
    assert w.summary_total("reader_skipped_upgrade") == 1
    assert w.rec(w.pdf).extracted_by == "parser@2"


@pytest.mark.parametrize("why", sorted(ROUTED))
async def test_an_edited_pdf_whose_text_routes_still_reaches_the_reader(w, why):
    w.reader.outcomes["lab-report.pdf"] = ExtractionOutcome(status=ExtractionStatus.TEXT, text=prose(5),
                                                            pages_total=3, pages_sent=3, model_calls=1)
    upgraded(w, **{"lab-report.pdf": ROUTED[why]})
    w.drive.edit(w.pdf, b"%PDF-1.7\nnew text", modified_at=w.clock.now)
    w.clock.advance(3600)
    await w.settle()
    assert len(w.reader.requests) == 1 and w.summary_total("reader_skipped_upgrade") == 0
    assert w.rec(w.pdf).extracted_by == "reader@1"


async def test_a_reader_stamped_record_is_not_stale(w):
    w.repo.records[("u", w.pdf)] = replace(w.rec(w.pdf), extracted_by="reader@1", reader_family="pdf")
    parser = upgraded(w)
    await w.settle()
    assert w.rec(w.pdf).extracted_by == "reader@1" and not any(r.name == "lab-report.pdf" for r in parser.requests)


async def test_chunks_in_another_space_take_the_full_path(w):
    w.repo.records[("u", w.doc)] = replace(w.rec(w.doc), chunk_space="other-space/4")
    embeds = len(w.emb.calls)
    upgraded(w)
    await w.settle()
    assert w.summary_total("reread_same") <= 1   # only the PDF may stamp
    assert len(w.emb.calls) > embeds and w.rec(w.doc).chunk_space == "test-space/8"


async def test_a_demoted_parser_record_is_an_upgrade_re_read_too(w):
    demoted = replace(w.rec(w.pdf), state=RecordState.METADATA_ONLY, reason=Reason.NO_TEXT, indexed_generation=None,
                      indexed_version=None, chunk_space="", chunk_count=0, dirty_since=None)
    w.repo.records[("u", w.pdf)] = demoted
    upgraded(w, **{"lab-report.pdf": thin()})
    await w.settle()
    rec = w.rec(w.pdf)
    assert len(w.reader.requests) == 0 and w.summary_total("reader_skipped_upgrade") == 1
    assert rec.state is RecordState.INDEXED and rec.extracted_by == "parser@2"   # the parser's text is kept
    assert any("LAB-2025-03-0417" in c.text for c in w.readable(w.pdf))


async def test_an_inaccessible_record_is_not_re_read():
    world = ClosedWorld()
    doc = world.add_doc("ipo-report.md", prose(40))
    await world.settle()
    world.drive.closed.add(doc)
    parser = upgraded(world)
    await world.settle()
    assert not any(r.name == "ipo-report.md" for r in parser.requests)
    assert world.rec(doc).extracted_by == "parser@1" and world.summary_total("reread_same") == 0

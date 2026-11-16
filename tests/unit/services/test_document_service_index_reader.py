"""DocumentService.read with `caller=CALLER_INDEX` and `reader_pages` (email index v2, slice 5): the reader runs for a
scan the parser's page count fits in the allowance, and only then (all or nothing). The real service over scripted
extractors; invented cast."""
import time

from src.domain.document_extraction import ExtractionStatus
from src.domain.document_read import CALLER_CONNECTOR, CALLER_INDEX, ReadRequest, ReadStatus, reader_skipped
from src.domain.drive_extraction import ExtractionOutcome
from tests.unit.services.test_document_service_read import FakeExtractor, make

PDF = b"%PDF-1.7\n" + b"x" * 64


def scan(pages: int) -> ExtractionOutcome:
    return ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=pages, extracted_by="parser@3")


READ = ExtractionOutcome(status=ExtractionStatus.TEXT, text="Laboratory report 0417: all values in range.",
                         pages_total=3, pages_read=3, model_calls=1, model_cost=0.002, extracted_by="reader@2")
TEXT_PDF = ExtractionOutcome(status=ExtractionStatus.TEXT, text="Reservation RM-55120 is confirmed. " * 40,
                             pages_total=1, extracted_by="parser@3")


def req(caller: str = CALLER_INDEX, reader_pages: int = 0, name: str = "LAB-2025-03-0417.pdf") -> ReadRequest:
    return ReadRequest(content=PDF, name=name, declared_mime="application/pdf", store="email", caller=caller,
                       native_formats=(), user_id="u1", account_id="acc1", deadline_at=time.time() + 60,
                       request_id="r1", reader_pages=reader_pages)


def rig(parsed: ExtractionOutcome):
    return make(parser=FakeExtractor("parser", parsed), reader=FakeExtractor("reader", READ))


async def test_no_reader_pages_means_the_reader_never_runs():
    service, _, reader, _ = rig(scan(3))
    out = await service.read(req(reader_pages=0))
    assert reader.requests == [] and reader_skipped(out) and out.pages_total == 3


async def test_a_three_page_scan_within_the_allowance_is_read_once_with_that_page_limit():
    service, parser, reader, _ = rig(scan(3))
    out = await service.read(req(reader_pages=3))
    assert len(reader.requests) == 1 and reader.requests[0].max_model_pages == 3
    assert out.status is ReadStatus.TEXT and out.text == READ.text
    assert out.links == ("parser@3", "reader@2") and out.model_cost_usd == 0.002


async def test_a_scan_over_the_allowance_is_not_read_at_all():
    service, _, reader, _ = rig(scan(12))
    out = await service.read(req(reader_pages=10))
    assert reader.requests == [] and out.status is ReadStatus.NO_TEXT and reader_skipped(out)


async def test_a_scan_with_an_unknown_page_count_is_skipped():
    service, _, reader, _ = rig(scan(0))
    out = await service.read(req(reader_pages=10))
    assert reader.requests == [] and reader_skipped(out)


async def test_a_connector_call_never_runs_the_reader_whatever_it_carries():
    service, _, reader, _ = rig(scan(3))
    out = await service.read(req(caller=CALLER_CONNECTOR, reader_pages=5))
    assert reader.requests == [] and reader_skipped(out)


async def test_a_text_pdf_reads_from_the_parser_without_the_reader():
    service, _, reader, _ = rig(TEXT_PDF)
    out = await service.read(req(reader_pages=10, name="RM-55120.pdf"))
    assert out.status is ReadStatus.TEXT and out.links == ("parser@3",) and reader.requests == []

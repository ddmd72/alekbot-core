"""DocumentService.read with `caller=CALLER_INDEX` (the email index): like the connector, the paid model reader never
runs; what the router would hand it answers NO_TEXT with a `READER_SKIPPED` detail. Invented cast."""
import time

import pytest

from src.domain.document_extraction import ExtractionStatus
from src.domain.document_read import CALLER_INDEX, CALLER_OPEN, ReadRequest, ReadStatus, reader_skipped
from src.domain.drive_extraction import ExtractionOutcome, ParserBusy
from tests.unit.services.test_document_service_read import FakeExtractor, make

PDF = b"%PDF-1.7\n" + b"x" * 64
TEXT_OUT = ExtractionOutcome(status=ExtractionStatus.TEXT, text="Lab report 0417 is ready. " * 40, pages_total=1,
                             extracted_by="parser@3")
SCAN_OUT = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=40, extracted_by="parser@3")
THIN_OUT = ExtractionOutcome(status=ExtractionStatus.TEXT, text="0417", pages_total=40, extracted_by="parser@3")
ERROR_OUT = ExtractionOutcome(status=ExtractionStatus.ERROR, detail="broken xref", extracted_by="parser@3")
LOCKED_OUT = ExtractionOutcome(status=ExtractionStatus.ENCRYPTED, extracted_by="parser@3")


def req(caller: str) -> ReadRequest:
    return ReadRequest(content=PDF, name="LAB-2025-03-0417.pdf", declared_mime="application/pdf", store="email",
                       caller=caller, native_formats=(), user_id="u1", account_id="acc1",
                       deadline_at=time.time() + 60, request_id="r1")


def test_the_caller_value():
    assert CALLER_INDEX == "index"


@pytest.mark.parametrize("parsed", [SCAN_OUT, THIN_OUT, ERROR_OUT])
async def test_a_pdf_the_router_would_hand_to_the_reader_is_not_read(parsed):
    service, parser, reader, _ = make(parser=FakeExtractor("parser", parsed))
    out = await service.read(req(CALLER_INDEX))
    assert reader.requests == [] and len(parser.requests) == 1
    assert out.status is ReadStatus.NO_TEXT and reader_skipped(out) and out.model_calls == 0


async def test_a_text_pdf_reads_as_for_the_chat():
    index, _, index_reader, _ = make(parser=FakeExtractor("parser", TEXT_OUT))
    chat, _, _, _ = make(parser=FakeExtractor("parser", TEXT_OUT))
    out = await index.read(req(CALLER_INDEX))
    assert out == await chat.read(req(CALLER_OPEN))
    assert out.status is ReadStatus.TEXT and index_reader.requests == []


async def test_an_encrypted_file_is_encrypted_and_the_reader_is_not_called():
    service, _, reader, _ = make(parser=FakeExtractor("parser", LOCKED_OUT))
    out = await service.read(req(CALLER_INDEX))
    assert out.status is ReadStatus.ENCRYPTED and reader.requests == []


async def test_a_busy_parser_stays_parser_unavailable():
    service, _, reader, _ = make(parser=FakeExtractor("parser", ParserBusy()))
    out = await service.read(req(CALLER_INDEX))
    assert out.status is ReadStatus.PARSER_UNAVAILABLE and out.detail == "busy" and reader.requests == []

"""DocumentService.read with `caller=CALLER_CONNECTOR` (the drive connector; owner decision 2026-11-03): the paid model
reader never runs. What the router would hand it (a scan, a thin or garbled text layer, a reader-first image) answers
NO_TEXT with a `READER_SKIPPED` detail and the parser's links; a text PDF reads exactly as for the chat; the chat's and
the prefetch's callers still reach the reader. Fake extractors with semantics; invented cast."""
import time

import pytest

from src.domain.document_extraction import ExtractionStatus
from src.domain.document_read import (
    CALLER_CONNECTOR, CALLER_OPEN, CALLER_PREFETCH, READER_SKIPPED, ReadOutcome, ReadRequest, ReadStatus,
    reader_skipped,
)
from src.domain.drive_extraction import ExtractionOutcome, ParserBusy
from tests.unit.services.test_document_service_read import FakeExtractor, make

PDF = b"%PDF-1.7\n" + b"x" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
TEXT_OUT = ExtractionOutcome(status=ExtractionStatus.TEXT, text="Invoice FA-0137 for 12 Oak Street is paid. " * 40,
                             pages_total=1, extracted_by="parser@3")
SCAN_OUT = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=40, extracted_by="parser@3")
THIN_OUT = ExtractionOutcome(status=ExtractionStatus.TEXT, text="FA-0137", pages_total=40, extracted_by="parser@3")
PARSER_ERROR_OUT = ExtractionOutcome(status=ExtractionStatus.ERROR, detail="broken xref", extracted_by="parser@3")


def req(content: bytes, name: str, mime: str, caller: str, *, store: str = "drive") -> ReadRequest:
    return ReadRequest(content=content, name=name, declared_mime=mime, store=store, caller=caller, native_formats=(),
                       user_id="u1", account_id="acc1", deadline_at=time.time() + 60, request_id="r1")


@pytest.mark.parametrize("parsed, route", [(SCAN_OUT, "no_text"), (THIN_OUT, "low_density"),
                                           (PARSER_ERROR_OUT, "parser_error")])
async def test_a_pdf_the_router_would_hand_to_the_reader_skips_it(parsed, route):
    service, parser, reader, _ = make(parser=FakeExtractor("parser", parsed))
    out = await service.read(req(PDF, "FA-0137.pdf", "application/pdf", CALLER_CONNECTOR))
    assert reader.requests == [] and len(parser.requests) == 1
    assert out.status is ReadStatus.NO_TEXT and out.detail == f"{READER_SKIPPED}: {route}" and reader_skipped(out)
    assert out.text == "" and out.links == ("parser@3",) and out.model_calls == 0 and out.model_cost_usd == 0.0
    assert out.pages_total == parsed.pages_total and out.kind == "pdf"


async def test_an_image_never_reaches_the_reader():
    service, parser, reader, _ = make()
    out = await service.read(req(PNG, "12 Oak Street.png", "image/png", CALLER_CONNECTOR))
    assert reader.requests == [] and parser.requests == []
    assert reader_skipped(out) and out.detail == f"{READER_SKIPPED}: image" and out.links == ()


async def test_a_text_pdf_reads_as_for_the_chat():
    connector, _, connector_reader, _ = make(parser=FakeExtractor("parser", TEXT_OUT))
    chat, _, chat_reader, _ = make(parser=FakeExtractor("parser", TEXT_OUT))
    out = await connector.read(req(PDF, "FA-0137.pdf", "application/pdf", CALLER_CONNECTOR))
    assert out == await chat.read(req(PDF, "FA-0137.pdf", "application/pdf", CALLER_OPEN))
    assert out.status is ReadStatus.TEXT and out.text == TEXT_OUT.text and not reader_skipped(out)
    assert connector_reader.requests == chat_reader.requests == []


async def test_a_busy_parser_stays_parser_unavailable():
    service, _, reader, _ = make(parser=FakeExtractor("parser", ParserBusy()))
    out = await service.read(req(PDF, "FA-0137.pdf", "application/pdf", CALLER_CONNECTOR))
    assert out.status is ReadStatus.PARSER_UNAVAILABLE and reader.requests == [] and not reader_skipped(out)


@pytest.mark.parametrize("caller", [CALLER_OPEN, CALLER_PREFETCH])
async def test_the_chat_and_the_prefetch_still_reach_the_reader(caller):
    service, _, reader, _ = make(parser=FakeExtractor("parser", SCAN_OUT))
    out = await service.read(req(PDF, "FA-0137.pdf", "application/pdf", caller, store="chat"))
    assert len(reader.requests) == 1
    assert out.status is ReadStatus.TEXT and out.text == "read by model" and out.model_calls == 2
    assert not reader_skipped(out)


@pytest.mark.parametrize("caller", [CALLER_OPEN, CALLER_PREFETCH])
async def test_an_image_still_reaches_the_reader_for_the_chat(caller):
    service, _, reader, _ = make()
    out = await service.read(req(PNG, "12 Oak Street.png", "image/png", caller, store="chat"))
    assert len(reader.requests) == 1 and out.status is ReadStatus.TEXT


@pytest.mark.parametrize("outcome", [
    ReadOutcome(status=ReadStatus.NO_TEXT),
    ReadOutcome(status=ReadStatus.NO_TEXT, detail="reader_skippedness"),
    ReadOutcome(status=ReadStatus.UNSUPPORTED, detail=f"{READER_SKIPPED}: no_text"),
])
def test_only_a_no_text_with_the_skip_detail_is_a_skip(outcome):
    assert not reader_skipped(outcome)

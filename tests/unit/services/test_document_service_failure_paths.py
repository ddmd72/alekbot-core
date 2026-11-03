"""DocumentService.read failure and edge paths of EXTERNAL_FILES_RFC §4.4 not covered by the happy-path files: each ends
in an honest status AND the log line the alerting reads. Fake ports with semantics; names are the invented cast."""
import asyncio
import logging
import time

import pytest

from src.domain.audio_transcript import AudioTranscript
from src.domain.document_extraction import ExtractionStatus
from src.domain.document_limits import MAX_FILE_BYTES
from src.domain.document_read import ReadRequest, ReadStatus
from src.domain.drive_extraction import ExtractionOutcome, ParserBusy, PdfSplitFailed
from src.services.document_service import DocumentService
from tests.unit.services.test_document_service_audio import AUDIO, FakeCache, FakeTranscriber
from tests.unit.services.test_document_service_audio import req as audio_req
from tests.unit.services.test_document_service_read import (
    PDF, PDF_NATIVE, PNG_NATIVE, SCAN_OUT, FakeExtractor, FakeSplitter, make, req,
)

S = ExtractionStatus


async def test_a_crash_before_admission_is_an_error_with_its_label_and_an_error_log(caplog):
    service, *_ = make()
    broken = ReadRequest(content=None, name="scan_0012.pdf", declared_mime="application/pdf", store="chat",  # type: ignore[arg-type]
                         caller="open", native_formats=(), user_id="ann", account_id="ann",
                         deadline_at=time.time() + 60, request_id="r1")
    with caplog.at_level(logging.WARNING):
        out = await service.read(broken)
    assert out.status is ReadStatus.ERROR and out.detail
    assert any(r.levelno == logging.ERROR and "read error" in r.getMessage() for r in caplog.records)


async def test_a_deadline_error_raised_inside_the_pipeline_is_a_timeout_with_a_warning(caplog):
    service, *_ = make(parser=FakeExtractor("parser", SCAN_OUT),
                       reader=FakeExtractor("reader", asyncio.TimeoutError()))
    with caplog.at_level(logging.WARNING):
        out = await service.read(req(PDF, "scan_0012.pdf", "application/pdf"))
    assert out.status is ReadStatus.TIMEOUT and out.detail == "deadline"
    assert any(r.levelno == logging.WARNING and "timeout" in r.getMessage() for r in caplog.records)


async def test_a_native_page_count_failure_other_than_encryption_keeps_its_status(caplog):
    service, parser, *_ = make(splitter=FakeSplitter(error=PdfSplitFailed(S.ERROR, "bad xref")))
    with caplog.at_level(logging.WARNING):
        out = await service.read(req(PDF, "scan_0012.pdf", "application/pdf", native=PDF_NATIVE))
    assert out.status is ReadStatus.ERROR and out.detail == "bad xref" and parser.requests == []
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_a_page_heavy_native_pdf_over_the_parse_cap_is_too_large_not_staged():
    big = PDF + b"x" * MAX_FILE_BYTES
    service, parser, reader, _ = make(splitter=FakeSplitter(pages=101))
    out = await service.read(req(big, "scan_0012.pdf", "application/pdf", native=PDF_NATIVE))
    assert out.status is ReadStatus.TOO_LARGE and out.detail.endswith("bytes for kind pdf")
    assert parser.requests == reader.requests == []


async def test_a_zip_document_over_the_parse_cap_is_too_large_and_never_staged():
    service, parser, *_ = make()
    out = await service.read(req(b"PK\x03\x04" + b"x" * MAX_FILE_BYTES, "lease_2026.docx",
                                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))
    assert out.status is ReadStatus.TOO_LARGE and parser.requests == []


async def test_a_reader_that_cannot_get_a_parser_slot_is_parser_unavailable():
    service, *_ = make(parser=FakeExtractor("parser", SCAN_OUT), reader=FakeExtractor("reader", ParserBusy()))
    out = await service.read(req(PDF, "scan_0012.pdf", "application/pdf"))
    assert out.status is ReadStatus.PARSER_UNAVAILABLE and out.detail == "busy"


async def test_a_busy_parser_outcome_is_parser_unavailable_not_a_timeout():
    busy = ExtractionOutcome(status=S.TIMEOUT, detail="busy")
    service, _, reader, _ = make(parser=FakeExtractor("parser", busy))
    out = await service.read(req(PDF, "scan_0012.pdf", "application/pdf"))
    assert out.status is ReadStatus.PARSER_UNAVAILABLE and out.detail == "busy" and reader.requests == []


async def test_a_budget_exhausted_outcome_is_never_a_silent_success(caplog):
    service, *_ = make(parser=FakeExtractor("parser", ExtractionOutcome(status=S.BUDGET_EXHAUSTED)))
    with caplog.at_level(logging.WARNING):
        out = await service.read(req(PDF, "scan_0012.pdf", "application/pdf"))
    assert out.status is ReadStatus.ERROR and out.detail == "budget exhausted (unreachable)"
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_a_native_declaration_for_another_mime_does_not_make_a_pdf_native():
    service, parser, *_ = make(splitter=FakeSplitter(pages=3))
    out = await service.read(req(PDF, "scan_0012.pdf", "application/pdf", native=PNG_NATIVE))
    assert out.status is ReadStatus.TEXT and len(parser.requests) == 1


async def test_a_whitespace_only_text_file_has_no_text():
    service, *_ = make()
    out = await service.read(req(b"  \n\t \n", "gym_pass_uk.md", "text/markdown"))
    assert out.status is ReadStatus.NO_TEXT and out.text == ""


async def test_a_blank_transcript_is_no_text_billed_and_not_cached():
    from tests.unit.services.test_document_service_audio import FakeQuota
    blank = AudioTranscript(text="  ", model="gpt-transcribe", seconds=60.0)
    transcriber, cache, quota = FakeTranscriber(answer=blank), FakeCache(), FakeQuota()
    service = DocumentService(FakeExtractor("parser"), FakeExtractor("reader"), FakeSplitter(),
                              transcriber=transcriber, transcript_cache=cache, quota=quota)
    out = await service.read(audio_req())
    assert out.status is ReadStatus.NO_TEXT and out.model_calls == 1 and out.model_cost_usd == pytest.approx(0.0045)
    assert cache.entries == {} and len(quota.bills) == 1


async def test_a_transcription_without_a_quota_service_is_an_error_log_and_still_read(caplog):
    service = DocumentService(FakeExtractor("parser"), FakeExtractor("reader"), FakeSplitter(),
                              transcriber=FakeTranscriber(), transcript_cache=FakeCache(), quota=None)
    with caplog.at_level(logging.WARNING):
        out = await service.read(audio_req())
    assert out.status is ReadStatus.TEXT and out.model_calls == 1
    assert any(r.levelno == logging.ERROR and "no quota service" in r.getMessage() and "not booked" in r.getMessage()
               for r in caplog.records)


async def test_an_audio_file_with_no_known_extension_is_sent_as_mpeg():
    seen = []

    class _Spy(FakeTranscriber):
        async def transcribe_metered(self, local_path, mime_type, languages=None):
            seen.append(mime_type)
            return await super().transcribe_metered(local_path, mime_type, languages)

    service = DocumentService(FakeExtractor("parser"), FakeExtractor("reader"), FakeSplitter(),
                              transcriber=_Spy(), transcript_cache=FakeCache(), quota=None)
    request = ReadRequest(content=AUDIO, name="voice_call", declared_mime="application/octet-stream", store="chat",
                          caller="open", native_formats=(), user_id="ann", account_id="ann",
                          deadline_at=time.time() + 60, request_id="r1")
    out = await service.read(request)
    assert out.status is ReadStatus.TEXT and seen == ["audio/mpeg"]

"""DocumentService.read (EXTERNAL_FILES_RFC §4.4 "read() in order"): kind from bytes, admission, caps, native, the
pipeline, the deadline, and the failure paths (each: an honest status AND an ERROR/WARNING log line). Fake ports with
semantics; the examples are the invented cast."""
import logging
import time
from typing import Callable, List, Optional, Sequence, Tuple

import pytest

from src.domain.document_extraction import ExtractionStatus
from src.domain.document_limits import MAX_FILE_BYTES, MAX_TEXT_EXPORT_BYTES
from src.domain.document_read import ReadRequest, ReadStatus
from src.domain.drive_extraction import (
    DriveModelUnavailable, ExtractionOutcome, ExtractionRequest, ParserBusy, PdfSplitFailed,
)
from src.domain.native_format import NativeFormat
from src.ports.content_extractor_port import ContentExtractorPort
from src.ports.pdf_splitter_port import PdfSplitterPort
from src.services.document_service import DocumentService

PDF = b"%PDF-1.7\n" + b"x" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
TEXT_OUT = ExtractionOutcome(status=ExtractionStatus.TEXT, text="a long enough text " * 50, pages_total=1,
                             extracted_by="parser@3")
SCAN_OUT = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=40, extracted_by="parser@3")


class FakeExtractor(ContentExtractorPort):
    """Answers a scripted outcome or raises a scripted exception; records requests and what the file looked like."""

    def __init__(self, extractor_id: str, answer: object = TEXT_OUT) -> None:
        self._id = extractor_id
        self.answer = answer
        self.requests: List[ExtractionRequest] = []

    @property
    def extractor_id(self) -> str:
        return self._id

    @property
    def version(self) -> int:
        return 1

    async def extract(self, request: ExtractionRequest) -> ExtractionOutcome:
        self.requests.append(request)
        if isinstance(self.answer, BaseException):
            raise self.answer
        assert isinstance(self.answer, ExtractionOutcome)
        return self.answer


class FakeSplitter(PdfSplitterPort):
    def __init__(self, pages: int = 3, error: Optional[BaseException] = None) -> None:
        self.pages = pages
        self.error = error
        self.calls = 0

    async def pdf_page_count(self, path: str, *, deadline_at: Optional[float] = None) -> int:
        self.calls += 1
        if self.error:
            raise self.error
        return self.pages

    async def cut_pdf_window(self, path: str, first_page: int, page_count: int, out_path: str,
                             *, deadline_at: Optional[float] = None) -> None:
        raise NotImplementedError

    async def cut_pdf_windows(self, path: str, windows: Sequence[Tuple[int, int]], out_paths: Sequence[str],
                              *, deadline_at: Optional[float] = None) -> None:
        raise NotImplementedError


def make(parser=None, reader=None, splitter=None) -> Tuple[DocumentService, FakeExtractor, FakeExtractor, FakeSplitter]:
    parser = parser or FakeExtractor("parser")
    reader = reader or FakeExtractor("reader", ExtractionOutcome(status=ExtractionStatus.TEXT, text="read by model",
                                                                 pages_total=40, pages_read=40, model_calls=2,
                                                                 model_cost=0.02, extracted_by="reader@2"))
    splitter = splitter or FakeSplitter()
    return DocumentService(parser, reader, splitter), parser, reader, splitter


def req(content: bytes, name: str, mime: str, *, store: str = "chat", native: Tuple[NativeFormat, ...] = (),
        deadline: float = 60.0) -> ReadRequest:
    return ReadRequest(content=content, name=name, declared_mime=mime, store=store, caller="open",
                       native_formats=native, user_id="ann", account_id="ann", deadline_at=time.time() + deadline,
                       request_id="r1")


PDF_NATIVE = (NativeFormat("application/pdf", 20 * 1024 * 1024, 100),)
PNG_NATIVE = (NativeFormat("image/png", 5 * 1024 * 1024, 1),)


async def test_a_scan_pdf_for_a_text_only_consumer_reaches_the_reader_with_the_d7_limits():
    service, parser, reader, _ = make(parser=FakeExtractor("parser", SCAN_OUT))
    out = await service.read(req(PDF, "scan_0012.pdf", "application/pdf"))
    assert out.status is ReadStatus.TEXT and out.text == "read by model"
    assert reader.requests[0].text_budget == 60_000 and reader.requests[0].max_model_pages == 200
    assert reader.requests[0].deadline_at is not None
    assert out.links == ("parser@3", "reader@2") and out.model_calls == 2 and out.model_cost_usd == 0.02
    assert out.pages_read == 40 and out.pages_total == 40 and out.kind == "pdf"


async def test_a_text_pdf_is_not_routed():
    service, parser, reader, _ = make()
    out = await service.read(req(PDF, "invoice_painting_es.pdf", "application/pdf"))
    assert out.status is ReadStatus.TEXT and out.links == ("parser@3",) and reader.requests == []


async def test_a_native_pdf_within_the_declaration_is_native_with_its_page_count():
    service, parser, reader, splitter = make(splitter=FakeSplitter(pages=3))
    out = await service.read(req(PDF, "scan_0012.pdf", "application/pdf", native=PDF_NATIVE))
    assert out.status is ReadStatus.NATIVE and out.native_mime == "application/pdf" and out.pages_total == 3
    assert parser.requests == reader.requests == []


async def test_a_native_pdf_over_the_page_limit_goes_the_pipeline():
    service, parser, reader, _ = make(splitter=FakeSplitter(pages=101))
    out = await service.read(req(PDF, "scan_0012.pdf", "application/pdf", native=PDF_NATIVE))
    assert out.status is ReadStatus.TEXT and len(parser.requests) == 1


async def test_an_encrypted_pdf_goes_the_pipeline_and_the_parser_names_it():
    enc = PdfSplitFailed(ExtractionStatus.ENCRYPTED)
    parser = FakeExtractor("parser", ExtractionOutcome(status=ExtractionStatus.ENCRYPTED))
    service, parser, reader, _ = make(parser=parser, splitter=FakeSplitter(error=enc))
    out = await service.read(req(PDF, "scan_0012.pdf", "application/pdf", native=PDF_NATIVE))
    assert out.status is ReadStatus.ENCRYPTED and len(parser.requests) == 1


async def test_a_native_pdf_between_the_parse_cap_and_its_declared_size_stays_native():
    big = PDF + b"x" * MAX_FILE_BYTES
    service, *_ = make()
    out = await service.read(req(big, "scan_0012.pdf", "application/pdf", native=PDF_NATIVE))
    assert out.status is ReadStatus.NATIVE


async def test_an_image_within_the_declaration_is_native_and_over_it_goes_to_the_reader():
    service, parser, reader, _ = make()
    out = await service.read(req(PNG, "scan_0012.png", "image/png", native=PNG_NATIVE))
    assert out.status is ReadStatus.NATIVE and out.native_mime == "image/png"
    small = (NativeFormat("image/png", 16, 1),)
    out = await service.read(req(PNG, "scan_0012.png", "image/png", native=small))
    assert out.status is ReadStatus.TEXT and len(reader.requests) == 1 and parser.requests == []
    assert reader.requests[0].mime_type == "image/png" and reader.requests[0].kind == "image"


async def test_a_renamed_file_is_read_by_its_sniffed_kind():
    service, parser, *_ = make()
    out = await service.read(req(PDF, "gym_pass_uk.txt", "text/plain"))
    assert out.kind == "pdf" and parser.requests[0].kind == "pdf"


async def test_an_executable_is_unsupported_with_a_warning(caplog):
    service, parser, reader, _ = make()
    with caplog.at_level(logging.WARNING):
        out = await service.read(req(b"MZ\x90\x00" + b"\x00" * 64, "gym_pass_uk.txt", "text/plain"))
    assert out.status is ReadStatus.UNSUPPORTED and out.kind == "executable"
    assert parser.requests == reader.requests == []
    assert any(r.levelno == logging.WARNING and "unsupported" in r.getMessage() for r in caplog.records)


async def test_plain_text_is_read_in_process_raw():
    service, parser, *_ = make()
    out = await service.read(req("# gym pass\nGym pass (Taras) 360 EUR\n".encode(), "gym_pass_uk.md", "text/markdown"))
    assert out.status is ReadStatus.TEXT and out.text.startswith("# gym pass") and parser.requests == []


async def test_a_text_mime_csv_and_eml_both_go_to_the_child():
    # U5 A3 (EXTERNAL_FILES_RFC §4.4): a `text/*` CSV is no longer read raw; it is the child's csv reader's
    service, parser, *_ = make()
    out = await service.read(req(b"a,b\n1,2\n", "gym_pass_uk.csv", "text/csv"))
    assert [r.kind for r in parser.requests] == ["csv"]
    assert out.text == TEXT_OUT.text and out.links == ("parser@3",)
    out = await service.read(req(b"From: ann\nSubject: gym\n\nhi", "gym_pass_uk.eml", "text/plain"))
    assert [r.kind for r in parser.requests] == ["csv", "eml"] and out.links == ("parser@3",)


async def test_a_document_kind_goes_to_the_parser():
    service, parser, *_ = make()
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w/>")
    out = await service.read(req(buf.getvalue(), "gym_pass_uk.docx", "application/octet-stream"))
    assert parser.requests[0].kind == "docx" and out.status is ReadStatus.TEXT


@pytest.mark.parametrize("store", ["chat", "drive", "workspace", "delivered", "email"])
async def test_parse_kinds_over_the_input_cap_are_too_large(store, caplog):
    service, parser, *_ = make()
    with caplog.at_level(logging.WARNING):
        out = await service.read(req(PDF + b"x" * MAX_FILE_BYTES, "scan_0012.pdf", "application/pdf", store=store))
    assert out.status is ReadStatus.TOO_LARGE and parser.requests == []
    assert any("too_large" in r.getMessage() for r in caplog.records)


async def test_text_has_the_two_stage_cap():
    service, *_ = make()
    out = await service.read(req(b"a" * (MAX_TEXT_EXPORT_BYTES + 1), "gym_pass_uk.md", "text/markdown"))
    assert out.status is ReadStatus.TOO_LARGE
    # Under the export cap, over the input cap with no data images: refused after the (no-op) drop.
    out = await service.read(req(b"a" * (MAX_FILE_BYTES + 1), "gym_pass_uk.md", "text/markdown"))
    assert out.status is ReadStatus.TOO_LARGE
    # Over the input cap only because of an inline image: the image is dropped first, so it reads.
    body = b"intro ![pic](data:image/png;base64," + b"A" * (MAX_FILE_BYTES + 10) + b") outro"
    out = await service.read(req(body, "gym_pass_uk.md", "text/markdown"))
    assert out.status is ReadStatus.TEXT and "AAAA" not in out.text and "outro" in out.text


async def test_parser_busy_is_parser_unavailable_with_an_error_log(caplog):
    service, *_ = make(parser=FakeExtractor("parser", ParserBusy()))
    with caplog.at_level(logging.WARNING):
        out = await service.read(req(PDF, "scan_0012.pdf", "application/pdf"))
    assert out.status is ReadStatus.PARSER_UNAVAILABLE and out.detail == "busy"
    assert any(r.levelno == logging.ERROR and "parser_unavailable" in r.getMessage() for r in caplog.records)


async def test_parser_busy_on_the_native_page_count_is_parser_unavailable(caplog):
    service, *_ = make(splitter=FakeSplitter(error=ParserBusy()))
    with caplog.at_level(logging.WARNING):
        out = await service.read(req(PDF, "scan_0012.pdf", "application/pdf", native=PDF_NATIVE))
    assert out.status is ReadStatus.PARSER_UNAVAILABLE and out.detail == "busy"
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_a_reader_outcome_of_busy_is_parser_unavailable():
    busy = ExtractionOutcome(status=ExtractionStatus.TIMEOUT, detail="busy")
    service, *_ = make(parser=FakeExtractor("parser", SCAN_OUT), reader=FakeExtractor("reader", busy))
    out = await service.read(req(PDF, "scan_0012.pdf", "application/pdf"))
    assert out.status is ReadStatus.PARSER_UNAVAILABLE and out.detail == "busy"


async def test_model_unavailable_carries_its_spend_and_logs_an_error(caplog):
    down = DriveModelUnavailable("429", model_calls=2, model_cost=0.05)
    service, *_ = make(parser=FakeExtractor("parser", SCAN_OUT), reader=FakeExtractor("reader", down))
    with caplog.at_level(logging.WARNING):
        out = await service.read(req(PDF, "scan_0012.pdf", "application/pdf"))
    assert out.status is ReadStatus.MODEL_UNAVAILABLE and out.model_calls == 2 and out.model_cost_usd == 0.05
    assert any(r.levelno == logging.ERROR and "model_unavailable" in r.getMessage() for r in caplog.records)


async def test_low_density_text_survives_a_model_outage_as_incomplete(caplog):
    thin = ExtractionOutcome(status=ExtractionStatus.TEXT, text="tiny", pages_total=40, extracted_by="parser@3")
    down = DriveModelUnavailable("5xx")
    service, *_ = make(parser=FakeExtractor("parser", thin), reader=FakeExtractor("reader", down))
    with caplog.at_level(logging.WARNING):
        out = await service.read(req(PDF, "scan_0012.pdf", "application/pdf"))
    assert out.status is ReadStatus.TEXT and out.text == "tiny" and out.may_be_incomplete
    assert out.links == ("parser@3",)
    assert any(r.levelno >= logging.WARNING and "unavailable" in r.getMessage() for r in caplog.records)


async def test_a_no_text_scan_does_not_fall_back_to_parser_text_on_outage():
    down = DriveModelUnavailable("5xx")
    service, *_ = make(parser=FakeExtractor("parser", SCAN_OUT), reader=FakeExtractor("reader", down))
    out = await service.read(req(PDF, "scan_0012.pdf", "application/pdf"))
    assert out.status is ReadStatus.MODEL_UNAVAILABLE


@pytest.mark.parametrize("status,expected,level", [
    (ExtractionStatus.MEMORY, ReadStatus.MEMORY, logging.ERROR),
    (ExtractionStatus.ERROR, ReadStatus.ERROR, logging.ERROR),
    (ExtractionStatus.TIMEOUT, ReadStatus.TIMEOUT, logging.WARNING),
    (ExtractionStatus.TOO_LARGE, ReadStatus.TOO_LARGE, logging.WARNING),
    (ExtractionStatus.UNSUPPORTED, ReadStatus.UNSUPPORTED, logging.WARNING),
    (ExtractionStatus.ENCRYPTED, ReadStatus.ENCRYPTED, logging.WARNING),
])
async def test_the_parser_failures_map_one_to_one_and_each_logs(status, expected, level, caplog):
    service, *_ = make(parser=FakeExtractor("parser", ExtractionOutcome(status=status, detail="label")))
    with caplog.at_level(logging.WARNING):
        out = await service.read(req(b"PK\x03\x04" + b"\x00" * 8, "gym_pass_uk.epub", "application/epub+zip"))
    assert out.status is expected
    assert any(r.levelno == level and expected.value in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("exc", [RuntimeError("boom"), OSError("disk"), ValueError("x")])
async def test_read_never_raises_whichever_port_raises(exc, caplog):
    cases = [(make(parser=FakeExtractor("parser", exc))[0], ()),
             (make(parser=FakeExtractor("parser", SCAN_OUT), reader=FakeExtractor("reader", exc))[0], ()),
             (make(splitter=FakeSplitter(error=exc))[0], PDF_NATIVE)]
    for service, native in cases:
        with caplog.at_level(logging.WARNING):
            out = await service.read(req(PDF, "scan_0012.pdf", "application/pdf", native=native))
        assert out.status is ReadStatus.ERROR and exc.__class__.__name__ in out.detail
    assert str(exc) not in caplog.text and any(r.levelno == logging.ERROR for r in caplog.records)


async def test_a_deadline_already_past_starts_no_work(caplog):
    service, parser, reader, splitter = make()
    with caplog.at_level(logging.WARNING):
        out = await service.read(req(PDF, "scan_0012.pdf", "application/pdf", native=PDF_NATIVE, deadline=-1.0))
    assert out.status is ReadStatus.TIMEOUT and out.detail == "deadline"
    assert parser.requests == reader.requests == [] and splitter.calls == 0
    assert any(r.levelno == logging.WARNING and "timeout" in r.getMessage() for r in caplog.records)


async def test_a_read_that_overruns_the_deadline_is_a_timeout(monkeypatch, caplog):
    import asyncio
    import src.services.document_service as module
    monkeypatch.setattr(module, "_DEADLINE_GRACE_S", 0.0)

    class Slow(FakeExtractor):
        async def extract(self, request):
            await asyncio.sleep(5)
            return TEXT_OUT

    service, *_ = make(parser=Slow("parser"))
    with caplog.at_level(logging.WARNING):
        out = await service.read(req(PDF, "scan_0012.pdf", "application/pdf", deadline=0.1))
    assert out.status is ReadStatus.TIMEOUT and out.detail == "deadline"
    assert any(r.levelno == logging.WARNING for r in caplog.records)


async def test_the_staged_file_is_removed_after_the_read():
    import os
    service, parser, *_ = make()
    await service.read(req(PDF, "scan_0012.pdf", "application/pdf"))
    assert not os.path.exists(parser.requests[0].path) and not os.path.exists(os.path.dirname(parser.requests[0].path))

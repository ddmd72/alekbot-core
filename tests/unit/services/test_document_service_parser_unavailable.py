"""A parser that cannot take the work (ParserUnavailable) or a slot that stays taken (ParserBusy) ends in a
PARSER_UNAVAILABLE outcome whose open note is the converter note, never a conversion error: from the page count, the
parser, the reader, and the reader's own page count. `read` never raises; the ERROR line is the alerting's."""
import logging

import pytest

from src.domain.document_extraction import ExtractionStatus
from src.domain.document_open_texts import parser_unavailable_note
from src.domain.document_presentation import note_for
from src.domain.document_read import ReadStatus
from src.domain.drive_extraction import ParserBusy, ParserUnavailable, PdfSplitFailed
from src.services.document_service import DocumentService
from tests.unit.composition.test_drive_reader_runner_halving import _runner as _reader_runner
from tests.unit.services.test_document_service_read import (
    PDF, PDF_NATIVE, SCAN_OUT, FakeExtractor, FakeSplitter, make, req,
)

NAME = "scan_0012.pdf"
MIME = "application/pdf"
REASONS = ["busy", "unavailable", "misconfigured"]


def _service_outcome_log(caplog):
    return [r for r in caplog.records if "[DocumentService] read parser_unavailable" in r.getMessage()]


@pytest.mark.parametrize("reason", REASONS)
async def test_the_native_page_count_ends_in_the_converter_note(reason, caplog):
    service, parser, *_ = make(splitter=FakeSplitter(error=ParserUnavailable(reason)))
    with caplog.at_level(logging.WARNING):
        out = await service.read(req(PDF, NAME, MIME, native=PDF_NATIVE))
    assert (out.status, out.detail) == (ReadStatus.PARSER_UNAVAILABLE, reason) and parser.requests == []
    assert note_for(out, NAME, MIME) == parser_unavailable_note(NAME)
    assert [r.levelno for r in _service_outcome_log(caplog)] == [logging.ERROR]


@pytest.mark.parametrize("reason", REASONS)
async def test_the_parser_ends_in_the_converter_note(reason, caplog):
    service, _, reader, _ = make(parser=FakeExtractor("parser", ParserUnavailable(reason)))
    with caplog.at_level(logging.WARNING):
        out = await service.read(req(PDF, NAME, MIME))
    assert (out.status, out.detail) == (ReadStatus.PARSER_UNAVAILABLE, reason) and reader.requests == []
    assert note_for(out, NAME, MIME) == parser_unavailable_note(NAME)
    assert [r.levelno for r in _service_outcome_log(caplog)] == [logging.ERROR]


@pytest.mark.parametrize("reason", REASONS)
async def test_the_reader_ends_in_the_converter_note_and_keeps_its_spend(reason, caplog):
    down = ParserUnavailable(reason, model_calls=2, model_cost=0.02)
    service, *_ = make(parser=FakeExtractor("parser", SCAN_OUT), reader=FakeExtractor("reader", down))
    with caplog.at_level(logging.WARNING):
        out = await service.read(req(PDF, NAME, MIME))
    assert (out.status, out.detail) == (ReadStatus.PARSER_UNAVAILABLE, reason)
    assert (out.model_calls, out.model_cost_usd) == (2, 0.02)
    assert note_for(out, NAME, MIME) == parser_unavailable_note(NAME)
    assert [r.levelno for r in _service_outcome_log(caplog)] == [logging.ERROR]


class _PageCountRaises:
    """The reader runner's splitter: the page count raises."""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def pdf_page_count(self, path, *, deadline_at=None):
        raise self.error


def _service_over_real_reader(error: BaseException):
    runner, _, _ = _reader_runner(lambda r: None)
    runner._splitter = _PageCountRaises(error)
    parser = FakeExtractor("parser", SCAN_OUT)
    return DocumentService(parser, runner, FakeSplitter())


async def test_a_busy_reader_page_count_answers_the_busy_note_not_a_conversion_note(caplog):
    service = _service_over_real_reader(ParserBusy())
    with caplog.at_level(logging.INFO):
        out = await service.read(req(PDF, NAME, MIME))
    assert (out.status, out.detail) == (ReadStatus.PARSER_UNAVAILABLE, "busy")
    assert note_for(out, NAME, MIME) == parser_unavailable_note(NAME)
    levels = {r.levelno for r in caplog.records if "page count: the parser is busy" in r.getMessage()}
    assert levels == {logging.WARNING}                       # the reader's own line
    assert [r.levelno for r in _service_outcome_log(caplog)] == [logging.ERROR]   # the service's alerting line
    assert not any("page count failed" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("reason", REASONS)
async def test_an_unavailable_reader_page_count_is_raised_not_turned_into_an_outcome(reason, caplog):
    service = _service_over_real_reader(ParserUnavailable(reason))
    with caplog.at_level(logging.INFO):
        out = await service.read(req(PDF, NAME, MIME))
    assert (out.status, out.detail) == (ReadStatus.PARSER_UNAVAILABLE, reason)
    assert note_for(out, NAME, MIME) == parser_unavailable_note(NAME)
    assert any(r.levelno == logging.WARNING and "the parser is unavailable" in r.getMessage() for r in caplog.records)


async def test_a_split_failure_other_than_busy_stays_the_files_outcome():
    service = _service_over_real_reader(PdfSplitFailed(ExtractionStatus.ENCRYPTED, "locked"))
    out = await service.read(req(PDF, NAME, MIME))
    assert out.status is ReadStatus.ENCRYPTED

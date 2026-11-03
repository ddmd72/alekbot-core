"""The two request limits and the read extent of an extraction (EXTERNAL_FILES_RFC §4.4, C, D7), and ParserBusy, the
parser's slot wait past a deadline (§4.4 "Parser slot")."""
import pytest

from src.domain.document_extraction import ExtractionStatus
from src.domain.drive_extraction import ExtractionOutcome, ExtractionRequest, ParserBusy, PdfSplitFailed


def _request(**kwargs) -> ExtractionRequest:
    return ExtractionRequest(path="/tmp/scan_0012.pdf", name="scan_0012.pdf", kind="pdf", mime_type="application/pdf",
                             user_id="ann", account_id="ann", **kwargs)


def test_a_request_without_limits_is_the_indexers():
    request = _request()
    assert request.deadline_at is None and request.text_budget is None


def test_a_request_carries_both_limits():
    request = _request(deadline_at=1_000.0, text_budget=60_000)
    assert (request.deadline_at, request.text_budget) == (1_000.0, 60_000)


def test_an_outcome_reports_no_read_extent_by_default():
    outcome = ExtractionOutcome(status=ExtractionStatus.TEXT, text="Lukas Weber owns the Q3 budget.")
    assert (outcome.pages_read, outcome.skipped_pages, outcome.cut_within_pages) == (0, (), ())


def test_an_outcome_carries_the_read_extent():
    outcome = ExtractionOutcome(status=ExtractionStatus.TEXT, pages_total=40, pages_read=10, skipped_pages=(4,),
                                cut_within_pages=tuple(range(11, 21)))
    assert outcome.pages_read == 10 and outcome.skipped_pages == (4,) and outcome.cut_within_pages[0] == 11


def test_parser_busy_is_a_split_failure_timing_out_busy():
    """The reader already handles PdfSplitFailed, so a busy parser reaches it as a timeout outcome."""
    busy = ParserBusy()
    assert isinstance(busy, PdfSplitFailed)
    assert busy.status is ExtractionStatus.TIMEOUT and busy.detail == "busy"
    assert str(busy) == "timeout: busy"
    with pytest.raises(PdfSplitFailed):
        raise busy

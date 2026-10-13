"""PDF text is extracted page by page and stops at MAX_TEXT_CHARS (round-2 review P-m6; USER_DRIVE_INDEXING_RFC §5.1,
§9). pdfminer's extract_text laid out every page before the cap cut the text: 1000 dense pages took 17.8 s and could
pass EXTRACT_WALL_TIMEOUT_S on the indexer's 1 vCPU, killing a file whose first 2M characters were readable."""
import pytest

import src.adapters.drive_parser.readers as readers
from src.adapters.drive_parser.readers import read_content
from src.domain.drive_extraction import ExtractionStatus
from tests.unit.adapters.drive_extraction_fixtures import make_pdf

_LINE = "Page {n:02d} of a long report with numbers and words"     # 50 characters


@pytest.fixture
def laid_out(monkeypatch):
    """The pages pdfminer laid out, counted at its interpreter."""
    from pdfminer.pdfinterp import PDFPageInterpreter

    pages = []
    original = PDFPageInterpreter.process_page

    def counting(self, page):
        pages.append(page)
        return original(self, page)

    monkeypatch.setattr(PDFPageInterpreter, "process_page", counting)
    return pages


def _pdf(tmp_path, count):
    return make_pdf(tmp_path / "long.pdf", [_LINE.format(n=n) for n in range(1, count + 1)])


def test_extraction_stops_at_the_cap_and_marks_truncated(tmp_path, monkeypatch, laid_out):
    monkeypatch.setattr(readers, "MAX_TEXT_CHARS", 200)
    outcome = read_content(str(_pdf(tmp_path, 40)), "pdf")
    assert outcome.status is ExtractionStatus.TEXT
    assert outcome.truncated is True
    assert outcome.pages_total == 40
    assert outcome.text.startswith("Page 01 of a long report")
    assert len(laid_out) < 10                        # four or five pages reach 200 characters, never all forty


def test_a_document_under_the_cap_is_read_whole_and_not_truncated(tmp_path, monkeypatch, laid_out):
    monkeypatch.setattr(readers, "MAX_TEXT_CHARS", 200_000)
    outcome = read_content(str(_pdf(tmp_path, 12)), "pdf")
    assert outcome.truncated is False
    assert len(laid_out) == 12
    assert "Page 12 of a long report" in outcome.text


def test_the_cap_reached_on_the_last_page_truncates_only_by_the_final_cut(tmp_path, monkeypatch, laid_out):
    whole = read_content(str(_pdf(tmp_path, 3)), "pdf").text
    raw_cap = len(whole) - 5                         # the cap is reached while the last page is laid out
    monkeypatch.setattr(readers, "MAX_TEXT_CHARS", raw_cap)
    laid_out.clear()
    outcome = read_content(str(_pdf(tmp_path, 3)), "pdf")
    assert len(laid_out) == 3
    assert outcome.truncated is True                 # _finish cuts the last 5 characters: that is truncation
    monkeypatch.setattr(readers, "MAX_TEXT_CHARS", len(whole) + 50)
    laid_out.clear()
    assert read_content(str(_pdf(tmp_path, 3)), "pdf").truncated is False


def test_a_memory_failure_while_a_page_is_laid_out_is_not_swallowed(tmp_path, monkeypatch):
    from pdfminer.pdfinterp import PDFPageInterpreter

    def exhausted(self, page):
        raise MemoryError("pdfminer")

    monkeypatch.setattr(PDFPageInterpreter, "process_page", exhausted)
    with pytest.raises(MemoryError):
        read_content(str(_pdf(tmp_path, 2)), "pdf")

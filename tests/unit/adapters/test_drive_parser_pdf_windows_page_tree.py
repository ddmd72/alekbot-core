"""Lazy PDF windows count pages from the page tree, never from /Count (arbiter R1-1; USER_DRIVE_INDEXING_RFC §24.3,
adversarial review A "m encrypted count"). pypdf answers `len(reader.pages)` of an encrypted file from the root's
/Count, which a file can set to anything: the reader would then ask for windows past the real last page."""
from pathlib import Path

import pytest

from src.adapters.drive_parser.readers import cut_pdf_window, pdf_page_count
from src.adapters.subprocess_content_extractor import SubprocessContentExtractor
from src.domain.drive_extraction import ExtractionStatus, PdfSplitFailed
from tests.unit.adapters.drive_extraction_fixtures import encrypt_pdf, make_pdf


def _page_texts(path):
    from pypdf import PdfReader

    return [page.extract_text().strip() for page in PdfReader(str(path)).pages]


def _lying_pdf(tmp_path: Path, lie: bytes = b"/Count 9\n") -> Path:
    """An owner-password-only PDF of two real pages whose page-tree root claims another count."""
    plain = make_pdf(tmp_path / "a.pdf", ["first page", "second page"])
    restricted = encrypt_pdf(plain, tmp_path / "restricted.pdf", user_password="").read_bytes()
    assert restricted.count(b"/Count 2\n") == 1
    lying = tmp_path / "lying.pdf"
    lying.write_bytes(restricted.replace(b"/Count 2\n", lie))
    return lying


@pytest.mark.parametrize("lie", [b"/Count 9\n", b"/Count -1", b"/Count 0\n"])
def test_page_count_of_a_lying_owner_password_pdf_is_its_real_two_pages(tmp_path, lie):
    assert pdf_page_count(str(_lying_pdf(tmp_path, lie))) == 2


def test_windows_cut_as_the_reader_does_cover_exactly_the_real_two_pages(tmp_path):
    source = str(_lying_pdf(tmp_path))
    texts = []
    for number, first_page in enumerate(range(0, pdf_page_count(source), 1)):   # one page per window
        out = tmp_path / f"w{number}.pdf"
        assert cut_pdf_window(source, first_page, 1, str(out)) == 1
        texts.extend(_page_texts(out))
    assert texts == ["first page", "second page"]


def test_one_window_over_the_whole_claimed_count_holds_the_real_pages_only(tmp_path):
    out = tmp_path / "w.pdf"
    assert cut_pdf_window(str(_lying_pdf(tmp_path)), 0, 10, str(out)) == 2
    assert _page_texts(out) == ["first page", "second page"]


def test_a_window_past_the_real_last_page_is_outside_the_document(tmp_path):
    with pytest.raises(PdfSplitFailed) as failed:
        cut_pdf_window(str(_lying_pdf(tmp_path)), 2, 1, str(tmp_path / "w.pdf"))
    assert failed.value.status is ExtractionStatus.ERROR


async def test_the_sandboxed_child_counts_the_page_tree(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    extractor = SubprocessContentExtractor(temp_root=str(root))
    assert await extractor.pdf_page_count(str(_lying_pdf(tmp_path))) == 2

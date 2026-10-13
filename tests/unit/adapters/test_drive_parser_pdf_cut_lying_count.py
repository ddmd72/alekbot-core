"""Windows are cut from the flattened page tree, never through `reader.pages[i]` (round-2 review P-M1;
USER_DRIVE_INDEXING_RFC §24.3). pypdf answers `reader.pages[i]` of an encrypted file from the root's /Count: an
owner-password PDF that claims /Count 0 failed every window, /Count 1 failed windows 2 and 3 with IndexError, and
/Count -1 failed with ValueError — while `pdf_page_count` said 3. The file came back `error`, was retried, and every
window read before the failure was billed again."""
from pathlib import Path

import pytest

from src.adapters.drive_parser.readers import cut_pdf_window, pdf_page_count
from tests.unit.adapters.drive_extraction_fixtures import encrypt_pdf, make_pdf

_PAGES = ["first page", "second page", "third page"]


def _page_texts(path: Path):
    from pypdf import PdfReader

    return [page.extract_text().strip() for page in PdfReader(str(path)).pages]


def _claims(tmp_path: Path, lie: bytes) -> Path:
    """An owner-password-only PDF of three real pages whose page-tree root claims `lie`."""
    plain = make_pdf(tmp_path / "a.pdf", _PAGES)
    restricted = encrypt_pdf(plain, tmp_path / "restricted.pdf", user_password="").read_bytes()
    assert restricted.count(b"/Count 3\n") == 1
    lying = tmp_path / "lying.pdf"
    lying.write_bytes(restricted.replace(b"/Count 3\n", lie))
    return lying


@pytest.mark.parametrize("lie", [b"/Count 0\n", b"/Count 1\n", b"/Count -1"])
def test_every_one_page_window_of_an_understated_count_is_cut(tmp_path, lie):
    source = str(_claims(tmp_path, lie))
    assert pdf_page_count(source) == 3
    texts = []
    for first_page in range(pdf_page_count(source)):
        out = tmp_path / f"w{first_page}.pdf"
        assert cut_pdf_window(source, first_page, 1, str(out)) == 1
        texts.extend(_page_texts(out))
    assert texts == _PAGES


@pytest.mark.parametrize("lie", [b"/Count 0\n", b"/Count 1\n", b"/Count -1"])
def test_one_window_over_an_understated_count_holds_every_real_page(tmp_path, lie):
    out = tmp_path / "w.pdf"
    assert cut_pdf_window(str(_claims(tmp_path, lie)), 0, 10, str(out)) == 3
    assert _page_texts(out) == _PAGES


@pytest.mark.parametrize("lie", [b"/Count 0\n", b"/Count 1\n", b"/Count -1"])
def test_a_middle_window_of_an_understated_count_is_its_own_pages(tmp_path, lie):
    out = tmp_path / "w.pdf"
    assert cut_pdf_window(str(_claims(tmp_path, lie)), 1, 2, str(out)) == 2
    assert _page_texts(out) == _PAGES[1:]

"""PDF table rows (EXTERNAL_FILES_RFC §7 U5 (2), A6): a ruled or aligned table becomes one pipe line per row, a page
without a table keeps pdfminer's text byte for byte. Fixtures are hand-written content streams."""
from pathlib import Path
from typing import List, Sequence, Tuple

import pytest

from src.adapters.drive_parser import readers
from src.adapters.drive_parser.readers import _pdfminer_text, read_content
from src.domain.document_extraction import ExtractionStatus
from tests.unit.adapters.drive_extraction_fixtures import encrypt_pdf, make_pdf

Cell = Tuple[float, float, str]          # x, y, text


def _write_pdf(path: Path, streams: Sequence[bytes]) -> Path:
    objects: List[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b"",
                            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for stream in streams:
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        content_id = len(objects)
        objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >>"
                       b" /Contents %d 0 R >>" % content_id)
        kids.append(b"%d 0 R" % len(objects))
    objects[1] = b"<< /Type /Pages /Kids [" + b" ".join(kids) + b"] /Count %d >>" % len(streams)
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    path.write_bytes(bytes(out))
    return path


def _text(cells: Sequence[Cell]) -> bytes:
    return b"".join(b"BT /F1 11 Tf %d %d Td (%s) Tj ET\n" % (x, y, t.encode("latin-1")) for x, y, t in cells)


def _grid(left: int, top: int, columns: Sequence[int], rows: int, height: int = 24) -> bytes:
    """Ruling lines of a table: columns are the x offsets of the vertical rules (relative to left)."""
    right, bottom = left + columns[-1], top - rows * height
    out = b"0.5 w\n"
    for i in range(rows + 1):
        y = top - i * height
        out += b"%d %d m %d %d l S\n" % (left, y, right, y)
    for x in columns:
        out += b"%d %d m %d %d l S\n" % (left + x, top, left + x, bottom)
    return out


INVOICE = [("Item", "Qty", "Amount"), ("Lock cylinder", "1", "199,00"), ("Key copy", "2", "25,00"),
           ("Total IVA incluido", "", "249,00")]


def _ruled_invoice(path: Path) -> Path:
    left, top, columns = 72, 600, [0, 220, 300, 400]
    text: List[Cell] = [(72, 700, "Invoice FA-0137 for Alex Doe, 12 Oak Street, Springfield")]
    for r, row in enumerate(INVOICE):
        for c, value in enumerate(row):
            if value:
                text.append((left + columns[c] + 6, top - r * 24 - 16, value))
    text.append((72, 440, "Paid on 15/09/2026."))
    return _write_pdf(path, [_grid(left, top, columns, len(INVOICE)) + _text(text)])


def test_a_ruled_invoice_keeps_each_row_on_one_line(tmp_path):
    out = read_content(str(_ruled_invoice(tmp_path / "ruled.pdf")), "pdf")
    assert out.status is ExtractionStatus.TEXT and out.pages_total == 1
    lines = out.text.splitlines()
    assert "| Lock cylinder | 1 | 199,00 |" in lines
    assert "| Key copy | 2 | 25,00 |" in lines
    assert "| Total IVA incluido |  | 249,00 |" in lines
    assert "Invoice FA-0137 for Alex Doe, 12 Oak Street, Springfield" in lines    # the prose around it stays
    assert "Paid on 15/09/2026." in lines


def test_an_unruled_aligned_invoice_becomes_rows(tmp_path):
    rows = [("Item", "Base", "Amount"), ("Tiling", "400,00", "400,00"), ("Plumbing", "452,40", "452,40"),
            ("IVA 23 %", "852,40", "196,05"), ("Total", "", "1.048,45")]
    text: List[Cell] = [(72, 700, "Bathroom repair invoice")]
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            if value:
                text.append((72 + (0, 200, 320)[c], 600 - r * 20, value))
    out = read_content(str(_write_pdf(tmp_path / "unruled.pdf", [_text(text)])), "pdf")
    lines = out.text.splitlines()
    assert any(line.startswith("| Plumbing |") and "452,40" in line for line in lines)
    assert any(line.startswith("| Total |") and line.rstrip(" |").endswith("1.048,45") for line in lines)


def test_a_two_column_prose_page_is_byte_identical_to_pdfminer(tmp_path):
    left = ["The garden committee met on Tuesday", "to discuss the shed permit and the", "water schedule for the summer",
            "Kim will call the council office", "Jordan will collect the signatures"]
    right = ["Alex proposed a new rota for the", "tool library, open on Saturdays", "from ten until noon, staffed by",
             "two volunteers each week, with", "a sign-in sheet at the door"]
    text = [(72, 700 - i * 16, t) for i, t in enumerate(left)] + [(330, 700 - i * 16, t) for i, t in enumerate(right)]
    path = _write_pdf(tmp_path / "prose.pdf", [_text(text)])
    assert read_content(str(path), "pdf").text == _pdfminer_text(str(path))[0].strip()


def test_a_plain_text_pdf_is_unchanged(tmp_path):
    path = make_pdf(tmp_path / "plain.pdf", ["Hello world", "Second page"])
    assert read_content(str(path), "pdf").text == _pdfminer_text(str(path))[0].strip()


def test_an_encrypted_pdf_is_still_encrypted(tmp_path):
    source = _ruled_invoice(tmp_path / "ruled.pdf")
    locked = encrypt_pdf(source, tmp_path / "locked.pdf", user_password="secret")
    assert read_content(str(locked), "pdf").status is ExtractionStatus.ENCRYPTED


@pytest.mark.slow
def test_two_hundred_pages_are_read_inside_the_wall(tmp_path):
    streams = [_grid(72, 600, [0, 220, 300, 400], len(INVOICE)) + _text(
        [(78 + (0, 220, 300)[c], 584 - r * 24, v) for r, row in enumerate(INVOICE) for c, v in enumerate(row[:3]) if v])
        for _ in range(200)]
    out = read_content(str(_write_pdf(tmp_path / "long.pdf", streams)), "pdf")
    assert out.status is ExtractionStatus.TEXT and out.pages_total == 200
    assert out.text.count("| Key copy | 2 | 25,00 |") == readers._MAX_TABLE_PAGES       # later table pages keep pdfminer's text
    assert out.text.count("Key copy") == 200


@pytest.mark.slow
def test_a_dense_prose_pdf_never_opens_pdfplumber(tmp_path, monkeypatch):
    import pdfplumber

    opened = []
    monkeypatch.setattr(pdfplumber, "open", lambda *a, **k: opened.append(a) or pytest.fail("pdfplumber opened"))
    lines = [f"Item {n} of the garden report with several plain words and a year 2026 inside".encode() for n in range(50)]
    stream = b"BT /F1 9 Tf 11 TL 40 760 Td\n" + b"\n".join(b"(" + line + b") '" for line in lines) + b"\nET"
    path = _write_pdf(tmp_path / "prose200.pdf", [stream] * 200)
    out = read_content(str(path), "pdf")
    assert out.status is ExtractionStatus.TEXT and out.pages_total == 200
    assert opened == [] and out.text == _pdfminer_text(str(path))[0].strip()

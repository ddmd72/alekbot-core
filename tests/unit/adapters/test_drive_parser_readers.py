"""Per-kind readers of the drive parser, run in-process (USER_DRIVE_INDEXING_RFC §5.1, §8.3, §9, §24.3). The
sandbox around them is tested in test_subprocess_content_extractor.py."""
import email.message
import email.policy

import pytest

from src.adapters.drive_parser import readers
from src.adapters.drive_parser.readers import (
    MARKITDOWN_CONVERTERS, cut_pdf_window, decode_text, markdown_table, pdf_page_count, read_content, subtitle_cues,
)
from src.domain.drive_extraction import ExtractionStatus, PdfSplitFailed
from src.domain.drive_index_policy import SLICE2_CONTENT_KINDS
from src.utils.document_hardening import build_markitdown
from tests.unit.adapters.drive_extraction_fixtures import (
    encrypt_pdf, make_docx, make_epub, make_inflation_bomb, make_member_bomb, make_odf, make_pdf, make_pptx,
    make_xlsx,
)

CYRILLIC = ("Договір оренди квартири укладено між сторонами на один рік. Орендна плата становить дванадцять тисяч "
            "гривень на місяць і сплачується до п'ятого числа кожного місяця.")


def _read(path, kind):
    return read_content(str(path), kind)


# =============================================================================
# Coverage: every admitted kind has a reader; markitdown only through the Phase 0 allowlist
# =============================================================================

def test_every_slice2_content_kind_has_a_reader():
    assert SLICE2_CONTENT_KINDS <= set(readers._READERS)


@pytest.mark.slow
def test_markitdown_converters_are_all_in_the_phase0_allowlist():
    registered = {type(reg.converter).__name__ for reg in build_markitdown()._converters}
    assert {name for name, _, _ in MARKITDOWN_CONVERTERS.values()} <= registered


@pytest.mark.parametrize("kind", ["doc", "ppt", "image", "archive", "iwork", "other"])
def test_kind_without_a_reader_is_unsupported(tmp_path, kind):
    path = tmp_path / "f.bin"
    path.write_bytes(b"x")
    assert _read(path, kind).status is ExtractionStatus.UNSUPPORTED


# =============================================================================
# Text kinds: charset detection (§5.1)
# =============================================================================

@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "cp1251"])
def test_text_is_decoded_whatever_the_charset(tmp_path, encoding):
    path = tmp_path / "note.txt"
    path.write_bytes(CYRILLIC.encode(encoding))
    outcome = _read(path, "text")
    assert outcome.status is ExtractionStatus.TEXT
    assert outcome.text == CYRILLIC


def test_utf32_with_bom(tmp_path):
    path = tmp_path / "note.txt"
    path.write_bytes(CYRILLIC.encode("utf-32"))
    assert _read(path, "text").text == CYRILLIC


@pytest.mark.parametrize("kind", ["markdown", "json"])
def test_markdown_and_json_are_read_as_text(tmp_path, kind):
    path = tmp_path / "f"
    path.write_text('# Title\n\n{"amount": "1 250,00"}', encoding="utf-8")
    assert _read(path, kind).text == '# Title\n\n{"amount": "1 250,00"}'


def test_empty_text_is_no_text(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_bytes(b"  \n\n \x00 ")
    assert _read(path, "text").status is ExtractionStatus.NO_TEXT


def test_text_is_normalised(tmp_path):
    path = tmp_path / "f.txt"
    path.write_bytes(b"a  \r\nb\x00\n\n\n\n\nc\n")
    assert _read(path, "text").text == "a\nb\n\nc"


def test_text_longer_than_the_cap_is_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr(readers, "MAX_TEXT_CHARS", 10)
    path = tmp_path / "long.txt"
    path.write_text("абвгдежзиклмнопрст", encoding="utf-8")       # 18 chars, 36 bytes > 10 * 4 bytes read
    outcome = _read(path, "text")
    assert outcome.truncated is True
    assert outcome.text == "абвгдежзик"


def test_decode_text_drops_a_character_split_by_the_prefix_cut():
    data = "абв".encode("utf-8")
    assert decode_text(data[:-1], cut=True) == "аб"
    assert decode_text(data, cut=False) == "абв"


# =============================================================================
# Subtitles (§8.1 step 6)
# =============================================================================

VTT = """WEBVTT
Kind: captions
Language: en

NOTE produced by a tool
spanning two lines

intro
00:00:01.000 --> 00:00:03.000
<v Alice>We agreed on the budget.</v>

00:00:03.000 --> 00:00:05.000 align:start
We agreed on the budget.

2
00:00:05.000 --> 00:00:07.000
<c.yellow>Next</c> step &amp; <00:00:06.000>owner
"""

SRT = """1
00:00:01,000 --> 00:00:02,000
Hello there.

2
00:00:02,500 --> 00:00:04,000
42

3
00:00:04,000 --> 00:00:05,000
Line one
line two
"""


def test_vtt_reduced_to_cue_text():
    assert subtitle_cues(VTT) == "Alice: We agreed on the budget.\nWe agreed on the budget.\nNext step & owner"


def test_srt_reduced_to_cue_text_and_a_numeric_cue_survives():
    assert subtitle_cues(SRT) == "Hello there.\n42\nLine one\nline two"


def test_subtitles_kind_reads_a_file(tmp_path):
    path = tmp_path / "talk.srt"
    path.write_text(SRT, encoding="utf-8")
    assert _read(path, "subtitles").text.startswith("Hello there.")


def test_rolled_up_repeats_are_dropped():
    assert subtitle_cues("1\n00:00 --> 00:01\nsame\n\n2\n00:01 --> 00:02\nsame\n") == "same"


# =============================================================================
# Tables: CSV, XLSX, XLS, ODS (§8.3)
# =============================================================================

def test_markdown_table_shape():
    table, truncated = markdown_table([["Name", "Amount", None], ["Rent", 12500.0, None], [None, None, None],
                                       ["Pipe | fee", 3.5, ""]])
    assert table == ("| Name | Amount |\n| --- | --- |\n| Rent | 12500 |\n| Pipe \\| fee | 3.5 |")
    assert truncated is False


def test_markdown_table_row_cap(monkeypatch):
    monkeypatch.setattr(readers, "MAX_SHEET_ROWS", 3)
    table, truncated = markdown_table([["h"], ["1"], ["2"], ["3"], ["4"]])
    assert truncated is True
    assert table.count("\n") == 3            # header, separator, two body rows


def test_markdown_table_column_cap(monkeypatch):
    monkeypatch.setattr(readers, "_MAX_TABLE_COLUMNS", 2)
    table, truncated = markdown_table([["a", "b", "c"]])
    assert truncated is True and table.startswith("| a | b |")


def test_csv_with_semicolons_becomes_a_table(tmp_path):
    path = tmp_path / "f.csv"
    rows = ["Дата;Призначення платежу;Сума"] + [f"2026-01-{d:02d};Оплата оренди квартири за місяць;1 250,00"
                                               for d in range(1, 8)]
    path.write_bytes("\n".join(rows).encode("cp1251"))
    outcome = _read(path, "csv")
    assert outcome.text.startswith("| Дата | Призначення платежу | Сума |\n| --- | --- | --- |\n"
                                   "| 2026-01-01 | Оплата оренди квартири за місяць | 1 250,00 |")


def test_csv_row_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(readers, "MAX_SHEET_ROWS", 5)
    path = tmp_path / "f.csv"
    path.write_text("\n".join(f"{i},{i * 2}" for i in range(100)), encoding="utf-8")
    outcome = _read(path, "csv")
    assert outcome.truncated is True
    assert outcome.text.count("\n") == 5


def test_xlsx_sheets_become_headed_tables(tmp_path):
    path = make_xlsx(tmp_path / "book.xlsx", [("Budget", [("Item", "Cost"), ("Rent", 1200)]),
                                              ("Empty", []), ("Notes", [("ok",)])])
    outcome = _read(path, "xlsx")
    assert outcome.status is ExtractionStatus.TEXT
    assert outcome.text == ("## Budget\n\n| Item | Cost |\n| --- | --- |\n| Rent | 1200 |\n\n"
                            "## Notes\n\n| ok |\n| --- |")


def test_xlsx_row_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(readers, "MAX_SHEET_ROWS", 10)
    path = make_xlsx(tmp_path / "big.xlsx", [("S", [(i,) for i in range(50)])])
    outcome = _read(path, "xlsx")
    assert outcome.truncated is True
    assert "| 9 |" in outcome.text and "| 10 |" not in outcome.text


class _XlsCell:
    def __init__(self, value, ctype=1):
        self.value, self.ctype = value, ctype


class _XlsSheet:
    def __init__(self, name, rows):
        self.name, self._rows, self.nrows = name, rows, len(rows)

    def row(self, index):
        return [_XlsCell(v, 2 if isinstance(v, float) else 1) for v in self._rows[index]]


class _XlsBook:
    datemode = 0

    def __init__(self, sheets):
        self._sheets, self.nsheets = sheets, len(sheets)
        self.released = False

    def sheet_by_index(self, index):
        return self._sheets[index]

    def unload_sheet(self, index):
        pass

    def release_resources(self):
        self.released = True


def test_xls_rows_capped_through_xlrd(tmp_path, monkeypatch):
    import xlrd

    book = _XlsBook([_XlsSheet("Old", [["Name", "Qty"]] + [[f"r{i}", float(i)] for i in range(30)])])
    monkeypatch.setattr(xlrd, "open_workbook", lambda path, on_demand: book)
    monkeypatch.setattr(readers, "MAX_SHEET_ROWS", 5)
    path = tmp_path / "old.xls"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    outcome = _read(path, "xls")
    assert outcome.truncated is True
    assert outcome.text.startswith("## Old\n\n| Name | Qty |")
    assert "| r3 | 3 |" in outcome.text and "| r4 |" not in outcome.text
    assert book.released is True


def test_encrypted_xls_is_encrypted(tmp_path, monkeypatch):
    import xlrd

    def refuse(path, on_demand):
        raise xlrd.XLRDError("Workbook is encrypted")

    monkeypatch.setattr(xlrd, "open_workbook", refuse)
    path = tmp_path / "locked.xls"
    path.write_bytes(b"x")
    assert _read(path, "xls").status is ExtractionStatus.ENCRYPTED


def test_broken_xls_is_an_error(tmp_path):
    path = tmp_path / "broken.xls"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100)
    assert _read(path, "xls").status is ExtractionStatus.ERROR


# =============================================================================
# markitdown kinds: PDF, DOCX, PPTX, EPUB, MSG, HTML
# =============================================================================

def test_pdf_text_and_page_count(tmp_path):
    path = make_pdf(tmp_path / "a.pdf", ["Invoice INV-2041 total 1250.00 EUR", "Second page"])
    outcome = _read(path, "pdf")
    assert outcome.status is ExtractionStatus.TEXT
    assert "Invoice INV-2041" in outcome.text and "Second page" in outcome.text
    assert outcome.pages_total == 2


def test_pdf_without_text_layer_is_no_text_with_its_page_count(tmp_path):
    outcome = _read(make_pdf(tmp_path / "scan.pdf", ["", "", ""]), "pdf")
    assert outcome.status is ExtractionStatus.NO_TEXT
    assert outcome.pages_total == 3


def test_pdf_with_user_password_is_encrypted(tmp_path):
    plain = make_pdf(tmp_path / "a.pdf", ["secret contract"])
    locked = encrypt_pdf(plain, tmp_path / "locked.pdf", user_password="pw")
    assert _read(locked, "pdf").status is ExtractionStatus.ENCRYPTED


def test_pdf_with_owner_password_only_is_read(tmp_path):
    plain = make_pdf(tmp_path / "a.pdf", ["printable contract"])
    restricted = encrypt_pdf(plain, tmp_path / "restricted.pdf", user_password="")
    outcome = _read(restricted, "pdf")
    assert outcome.status is ExtractionStatus.TEXT
    assert "printable contract" in outcome.text


def test_damaged_pdf_is_an_error(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4\nthis is not a pdf body")
    assert _read(path, "pdf").status is ExtractionStatus.ERROR


def test_docx(tmp_path):
    outcome = _read(make_docx(tmp_path / "a.docx", ["Penalty is 0.1% per day.", CYRILLIC]), "docx")
    assert outcome.status is ExtractionStatus.TEXT
    assert "Penalty is 0.1% per day." in outcome.text and CYRILLIC in outcome.text


def test_pptx(tmp_path):
    outcome = _read(make_pptx(tmp_path / "deck.pptx", ["Quarterly roadmap", "Hiring plan"]), "pptx")
    assert "Quarterly roadmap" in outcome.text and "Hiring plan" in outcome.text


def test_epub(tmp_path):
    outcome = _read(make_epub(tmp_path / "b.epub", "<h1>Chapter One</h1><p>It was a dark night.</p>"), "epub")
    assert outcome.status is ExtractionStatus.TEXT
    assert "It was a dark night." in outcome.text


def test_epub_with_drm_is_encrypted(tmp_path):
    encryption = ('<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
                  'xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData><enc:CipherData>'
                  '<enc:CipherReference URI="OEBPS/c1.xhtml"/></enc:CipherData></enc:EncryptedData></encryption>')
    path = make_epub(tmp_path / "drm.epub", "<p>x</p>", encryption_xml=encryption)
    assert _read(path, "epub").status is ExtractionStatus.ENCRYPTED


def test_epub_with_adobe_rights_is_encrypted(tmp_path):
    assert _read(make_epub(tmp_path / "r.epub", "<p>x</p>", rights=True), "epub").status is ExtractionStatus.ENCRYPTED


def test_epub_font_obfuscation_is_not_drm(tmp_path):
    encryption = ('<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
                  'xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData><enc:CipherData>'
                  '<enc:CipherReference URI="OEBPS/fonts/Serif.otf"/></enc:CipherData></enc:EncryptedData>'
                  '</encryption>')
    path = make_epub(tmp_path / "fonts.epub", "<p>Readable text.</p>", encryption_xml=encryption)
    assert "Readable text." in _read(path, "epub").text


def test_html_is_converted_never_read_raw(tmp_path):
    path = tmp_path / "report.html"
    path.write_text("<html><head><style>body{color:red}</style><script>alert('x')</script></head><body>"
                    "<h1>Report</h1><p>Total <b>42</b></p><img src='data:image/png;base64," + "A" * 500 + "'>"
                    "</body></html>", encoding="utf-8")
    text = _read(path, "html").text
    assert "# Report" in text and "Total **42**" in text
    assert "alert" not in text and "color:red" not in text and "A" * 100 not in text


def test_html_in_cp1251(tmp_path):
    path = tmp_path / "old.html"
    path.write_bytes(f"<html><body><p>{CYRILLIC}</p></body></html>".encode("cp1251"))
    assert CYRILLIC in _read(path, "html").text


def test_msg_goes_through_markitdowns_outlook_converter(tmp_path, monkeypatch):
    from markitdown import DocumentConverterResult
    from markitdown.converters import OutlookMsgConverter

    seen = {}

    def convert(self, stream, info, **kwargs):
        seen["extension"] = info.extension
        return DocumentConverterResult(markdown="# Email Message\n\n**Subject:** Lease")

    monkeypatch.setattr(OutlookMsgConverter, "convert", convert)
    path = tmp_path / "mail.msg"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    assert _read(path, "msg").text == "# Email Message\n\n**Subject:** Lease"
    assert seen["extension"] == ".msg"


def test_msg_that_is_not_ole_is_an_error(tmp_path):
    path = tmp_path / "fake.msg"
    path.write_bytes(b"not an outlook file")
    assert _read(path, "msg").status is ExtractionStatus.ERROR


# =============================================================================
# EML, RTF
# =============================================================================

def _eml(tmp_path, *, plain=None, html=None, charset="utf-8"):
    message = email.message.EmailMessage(policy=email.policy.default)
    message["Subject"] = "Оренда: рахунок за березень"
    message["From"] = "landlord@example.com"
    message["To"] = "tenant@example.com"
    if plain is not None:
        message.set_content(plain, charset=charset, cte="quoted-printable")
    if html is not None:
        if plain is None:
            message.set_content(html, subtype="html", cte="base64")
        else:
            message.add_alternative(html, subtype="html")
    path = tmp_path / "m.eml"
    path.write_bytes(message.as_bytes())
    return path


def test_eml_prefers_the_plain_part(tmp_path):
    text = _read(_eml(tmp_path, plain=CYRILLIC, html="<p>HTML VERSION</p>"), "eml").text
    assert text.startswith("Subject: Оренда: рахунок за березень\nFrom: landlord@example.com")
    assert CYRILLIC in text and "HTML VERSION" not in text


def test_eml_html_only_goes_through_the_html_converter(tmp_path):
    text = _read(_eml(tmp_path, html="<h2>Invoice</h2><script>x()</script><p>Due 2026-03-05</p>"), "eml").text
    assert "## Invoice" in text and "Due 2026-03-05" in text and "x()" not in text


def test_eml_in_cp1251_quoted_printable(tmp_path):
    assert CYRILLIC in _read(_eml(tmp_path, plain=CYRILLIC, charset="cp1251"), "eml").text


def test_rtf_with_codepage(tmp_path):
    escaped = "".join(f"\\'{b:02x}" for b in "Договір".encode("cp1251"))
    path = tmp_path / "a.rtf"
    path.write_bytes(("{\\rtf1\\ansi\\ansicpg1251{\\fonttbl{\\f0 Arial;}}\\f0 " + escaped + " \\par Rent}")
                     .encode("ascii"))
    outcome = _read(path, "rtf")
    assert outcome.status is ExtractionStatus.TEXT
    assert "Договір" in outcome.text and "Rent" in outcome.text


# =============================================================================
# ODF via defusedxml
# =============================================================================

def test_odt_headings_paragraphs_and_tables(tmp_path):
    body = ('<office:text><text:h text:outline-level="2">Terms</text:h>'
            '<text:p>Rent is<text:s text:c="2"/>due<text:tab/>monthly.<text:note><text:note-body>'
            '<text:p>footnote</text:p></text:note-body></text:note></text:p>'
            '<table:table table:name="T"><table:table-row><table:table-cell><text:p>A</text:p></table:table-cell>'
            '<table:table-cell><text:p>B</text:p></table:table-cell></table:table-row></table:table>'
            '</office:text>')
    text = _read(make_odf(tmp_path / "a.odt", "odt", body), "odt").text
    assert text == "## Terms\n\nRent is  due\tmonthly.\n\n| A | B |\n| --- | --- |"


def test_ods_sheets_with_hostile_repeats_stay_bounded(tmp_path):
    body = ('<office:spreadsheet><table:table table:name="Budget">'
            '<table:table-row><table:table-cell><text:p>Item</text:p></table:table-cell>'
            '<table:table-cell><text:p>Cost</text:p></table:table-cell>'
            '<table:table-cell table:number-columns-repeated="16000"/></table:table-row>'
            '<table:table-row><table:table-cell><text:p>Rent</text:p></table:table-cell>'
            '<table:table-cell><text:p>900</text:p></table:table-cell></table:table-row>'
            '<table:table-row table:number-rows-repeated="1048000"><table:table-cell '
            'table:number-columns-repeated="1024"/></table:table-row>'
            '</table:table></office:spreadsheet>')
    text = _read(make_odf(tmp_path / "b.ods", "ods", body), "ods").text
    assert text == "## Budget\n\n| Item | Cost |\n| --- | --- |\n| Rent | 900 |"


def test_ods_repeated_value_rows_are_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(readers, "MAX_SHEET_ROWS", 4)
    body = ('<office:spreadsheet><table:table table:name="S"><table:table-row table:number-rows-repeated="100000">'
            '<table:table-cell><text:p>x</text:p></table:table-cell></table:table-row></table:table>'
            '</office:spreadsheet>')
    outcome = _read(make_odf(tmp_path / "c.ods", "ods", body), "ods")
    assert outcome.truncated is True
    assert outcome.text.count("| x |") == 4


def test_odp_slides(tmp_path):
    body = ('<office:presentation><draw:page draw:name="Intro"><draw:frame><draw:text-box>'
            '<text:p>Welcome</text:p></draw:text-box></draw:frame></draw:page>'
            '<draw:page draw:name="Plan"><draw:frame><draw:text-box><text:p>Ship it</text:p></draw:text-box>'
            '</draw:frame></draw:page></office:presentation>')
    text = _read(make_odf(tmp_path / "d.odp", "odp", body), "odp").text
    assert text == "## Intro\n\nWelcome\n\n## Plan\n\nShip it"


def test_password_protected_odf_is_encrypted(tmp_path):
    path = make_odf(tmp_path / "e.odt", "odt", "<office:text/>", encrypted=True)
    assert _read(path, "odt").status is ExtractionStatus.ENCRYPTED


def test_odf_with_a_dtd_entity_is_refused_for_good(tmp_path):
    import zipfile

    path = tmp_path / "xxe.odt"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("content.xml", '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
                                  '<x>&e;</x>')
    assert _read(path, "odt").status is ExtractionStatus.UNSUPPORTED


# =============================================================================
# Containers: Phase 0 ZIP caps and encrypted OOXML
# =============================================================================

@pytest.mark.slow
@pytest.mark.parametrize("kind", ["docx", "xlsx", "pptx", "epub", "odt"])
def test_member_bomb_is_too_large_before_any_parser(tmp_path, kind):
    assert _read(make_member_bomb(tmp_path / f"bomb.{kind}"), kind).status is ExtractionStatus.TOO_LARGE


def test_inflation_bomb_is_too_large_before_any_parser(tmp_path):
    assert _read(make_inflation_bomb(tmp_path / "bomb.docx"), "docx").status is ExtractionStatus.TOO_LARGE


@pytest.mark.parametrize("kind", ["docx", "xlsx", "pptx"])
def test_encrypted_ooxml_package_is_encrypted(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(readers, "_ole_streams", lambda path: frozenset({"EncryptionInfo", "EncryptedPackage"}))
    path = tmp_path / f"locked.{kind}"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600)
    assert _read(path, kind).status is ExtractionStatus.ENCRYPTED


def test_ole_file_under_an_ooxml_kind_without_encryption_is_an_error(tmp_path):
    path = tmp_path / "legacy.docx"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600)
    assert _read(path, "docx").status is ExtractionStatus.ERROR


def test_docx_that_is_not_a_zip_is_unsupported(tmp_path):
    path = tmp_path / "plain.docx"
    path.write_bytes(b"just text")
    assert _read(path, "docx").status is ExtractionStatus.UNSUPPORTED


# =============================================================================
# Deterministic failures are terminal (`unsupported`, §8.2), never retried as `error`
# =============================================================================

def _markitdown_at_least(version):
    from importlib.metadata import version as installed

    return tuple(int(p) for p in installed("markitdown").split(".")[:3]) >= version


def test_html_nested_past_the_parsers_recursion_is_unsupported(tmp_path):
    """Hostile nesting never becomes a retried `error`. markitdown 0.1.8 (the requirements.txt pin, review A
    "M pins") catches the RecursionError itself and falls back to plain-text extraction, so the text is read; an
    older markitdown raises and the reader maps it to a terminal `unsupported` (§8.2). The mapping of a
    RecursionError raised by any reader stays pinned by test_deterministic_failures_of_any_reader_are_unsupported."""
    path = tmp_path / "nested.html"
    path.write_text("<html><body>" + "<div>" * 5000 + "deep" + "</div>" * 5000 + "</body></html>",
                    encoding="utf-8")
    outcome = _read(path, "html")
    if _markitdown_at_least((0, 1, 8)):
        assert outcome.status is ExtractionStatus.TEXT
        assert outcome.text == "deep"
    else:
        assert outcome.status is ExtractionStatus.UNSUPPORTED
        assert "RecursionError" in outcome.detail


@pytest.mark.parametrize("failure", [RecursionError("deep"), readers._WrongContainer("ole")])
def test_deterministic_failures_of_any_reader_are_unsupported(tmp_path, monkeypatch, failure):
    def explode(path, kind):
        raise failure

    monkeypatch.setitem(readers._READERS, "text", explode)
    path = tmp_path / "a.txt"
    path.write_text("x")
    assert _read(path, "text").status is ExtractionStatus.UNSUPPORTED


@pytest.mark.parametrize("failure", [ValueError("odd"), KeyError("content.xml"), OSError(5, "I/O error")])
def test_other_failures_stay_errors(tmp_path, monkeypatch, failure):
    def explode(path, kind):
        raise failure

    monkeypatch.setitem(readers._READERS, "text", explode)
    path = tmp_path / "a.txt"
    path.write_text("x")
    assert _read(path, "text").status is ExtractionStatus.ERROR


def test_split_hitting_recursion_is_unsupported(tmp_path, monkeypatch):
    import pypdf

    def deep(*args, **kwargs):
        raise RecursionError("page tree loop")

    monkeypatch.setattr(pypdf, "PdfReader", deep)
    for call in (lambda: pdf_page_count(str(tmp_path / "x.pdf")),
                 lambda: cut_pdf_window(str(tmp_path / "x.pdf"), 0, 10, str(tmp_path / "w.pdf"))):
        with pytest.raises(PdfSplitFailed) as failed:
            call()
        assert failed.value.status is ExtractionStatus.UNSUPPORTED


@pytest.mark.parametrize("failure", [
    MemoryError(),
    ImportError("libonnxruntime.so.1: failed to map segment from shared object"),
    OSError(12, "Cannot allocate memory"),
])
def test_memory_failures_propagate_to_the_child_as_memory_error(tmp_path, monkeypatch, failure):
    def explode(path, kind):
        raise failure

    monkeypatch.setitem(readers._READERS, "text", explode)
    path = tmp_path / "a.txt"
    path.write_text("x")
    with pytest.raises(MemoryError):
        _read(path, "text")


def test_an_ordinary_import_error_is_an_error_outcome(tmp_path, monkeypatch):
    def explode(path, kind):
        raise ImportError("No module named 'striprtf'")

    monkeypatch.setitem(readers._READERS, "rtf", explode)
    path = tmp_path / "a.rtf"
    path.write_text("x")
    assert _read(path, "rtf").status is ExtractionStatus.ERROR


# =============================================================================
# pdf_page_count + cut_pdf_window: lazy page windows, one cut per call (§24.3, review A "M split"). The month and
# MODEL_EXTRACT_MAX_PAGES caps (formerly split_pdf_windows' max_pages/truncated) live in DriveReaderRunner and are
# pinned in tests/unit/composition/test_drive_reader_runner*.py.
# =============================================================================

def _page_texts(path):
    from pypdf import PdfReader

    return [page.extract_text().strip() for page in PdfReader(str(path)).pages]


def test_split_cuts_windows_in_page_order(tmp_path):
    source = make_pdf(tmp_path / "s.pdf", [f"page {i}" for i in range(1, 26)])
    out = tmp_path / "w"
    out.mkdir()
    assert pdf_page_count(str(source)) == 25
    written = [cut_pdf_window(str(source), first, 10, str(out / f"window-{n:04d}.pdf"))
               for n, first in enumerate(range(0, 25, 10))]
    assert written == [10, 10, 5]
    assert sorted(p.name for p in out.iterdir()) == ["window-0000.pdf", "window-0001.pdf", "window-0002.pdf"]
    assert _page_texts(out / "window-0000.pdf") == [f"page {i}" for i in range(1, 11)]
    assert _page_texts(out / "window-0002.pdf") == [f"page {i}" for i in range(21, 26)]


def test_split_of_an_owner_password_pdf_works(tmp_path):
    source = encrypt_pdf(make_pdf(tmp_path / "s.pdf", ["a", "b"]), tmp_path / "r.pdf", user_password="")
    assert pdf_page_count(str(source)) == 2
    assert [cut_pdf_window(str(source), first, 1, str(tmp_path / f"w{first}.pdf")) for first in (0, 1)] == [1, 1]
    assert _page_texts(tmp_path / "w1.pdf") == ["b"]


def test_split_of_a_user_password_pdf_is_encrypted(tmp_path):
    source = encrypt_pdf(make_pdf(tmp_path / "s.pdf", ["a"]), tmp_path / "l.pdf", user_password="pw")
    for call in (lambda: pdf_page_count(str(source)),
                 lambda: cut_pdf_window(str(source), 0, 10, str(tmp_path / "w.pdf"))):
        with pytest.raises(PdfSplitFailed) as failed:
            call()
        assert failed.value.status is ExtractionStatus.ENCRYPTED


def test_split_of_garbage_is_an_error(tmp_path):
    source = tmp_path / "g.pdf"
    source.write_bytes(b"garbage")
    for call in (lambda: pdf_page_count(str(source)),
                 lambda: cut_pdf_window(str(source), 0, 10, str(tmp_path / "w.pdf"))):
        with pytest.raises(PdfSplitFailed) as failed:
            call()
        assert failed.value.status is ExtractionStatus.ERROR


def test_split_needs_a_positive_window(tmp_path):
    source = make_pdf(tmp_path / "s.pdf", ["a", "b"])
    with pytest.raises(PdfSplitFailed) as failed:
        cut_pdf_window(str(source), 0, 0, str(tmp_path / "w.pdf"))
    assert failed.value.status is ExtractionStatus.ERROR
    assert not (tmp_path / "w.pdf").exists()


@pytest.mark.parametrize("text,encoding", [
    ("Орендна плата 12 500,00 грн", "cp1251"),          # alone, the detector ranks mac_cyrillic first
    ("Дата;Сума\n2026-01-05;1 250,00\n", "cp1251"),     # alone, it ranks big5 first
    ("Ñandú y camión, año 2026", "cp1252"),             # cp1250 ties; Ñ differs
])
def test_short_legacy_texts_prefer_the_common_code_page(text, encoding):
    assert decode_text(text.encode(encoding)) == text


def test_a_real_cjk_text_is_not_forced_into_a_legacy_code_page():
    text = "这是一个关于房屋租赁合同的测试文件，其中包含租金和付款日期。" * 3
    assert decode_text(text.encode("gb18030")) == text


# =============================================================================
# XLSX stays bounded: columns capped while reading, sheets rendered one by one, reading stops at the text cap
# (review A, M xlsx)
# =============================================================================

def _wide_xlsx(path, rows):
    """`rows` rows, each with a value in column A and one in the last column Excel allows (XFD)."""
    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Wide"
    for index in range(1, rows + 1):
        sheet.cell(row=index, column=1, value=f"r{index}")
        sheet.cell(row=index, column=16384, value="far")
    book.save(str(path))
    return path


@pytest.mark.slow
def test_xlsx_with_a_far_right_cell_reads_in_bounded_memory(tmp_path):
    import tracemalloc

    path = _wide_xlsx(tmp_path / "wide.xlsx", 600)
    tracemalloc.start()
    try:
        outcome = _read(path, "xlsx")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert outcome.status is ExtractionStatus.TEXT
    assert "| r600 |" in outcome.text and "far" not in outcome.text
    assert peak < 40 * 1024 * 1024        # 600 rows padded to 16384 columns would hold ~80 MB of row tuples


class _RowSpy:
    """Counts the rows each sheet's iter_rows hands out."""

    def __init__(self, monkeypatch):
        from openpyxl.worksheet._read_only import ReadOnlyWorksheet

        self.rows = {}
        original = ReadOnlyWorksheet.iter_rows
        spy = self

        def counting(sheet, *args, **kwargs):
            spy.rows.setdefault(sheet.title, 0)
            for row in original(sheet, *args, **kwargs):
                spy.rows[sheet.title] += 1
                yield row

        monkeypatch.setattr(ReadOnlyWorksheet, "iter_rows", counting)


def test_xlsx_stops_reading_sheets_once_the_text_cap_is_reached(tmp_path, monkeypatch):
    monkeypatch.setattr(readers, "MAX_TEXT_CHARS", 300)
    spy = _RowSpy(monkeypatch)
    rows = [(f"line {i} " + "x" * 40,) for i in range(20)]
    path = make_xlsx(tmp_path / "many.xlsx", [("First", rows), ("Second", rows), ("Third", rows)])
    outcome = _read(path, "xlsx")
    assert outcome.truncated is True
    assert outcome.text.startswith("## First")
    assert "Second" not in spy.rows and "Third" not in spy.rows


def test_xlsx_stops_reading_rows_of_a_sheet_once_the_text_cap_is_reached(tmp_path, monkeypatch):
    monkeypatch.setattr(readers, "MAX_TEXT_CHARS", 300)
    spy = _RowSpy(monkeypatch)
    path = make_xlsx(tmp_path / "long.xlsx", [("Only", [(f"line {i} " + "x" * 40,) for i in range(200)])])
    outcome = _read(path, "xlsx")
    assert outcome.truncated is True
    assert spy.rows["Only"] < 20


def test_xlsx_value_in_the_first_column_past_the_cap_marks_truncated(tmp_path, monkeypatch):
    import openpyxl

    monkeypatch.setattr(readers, "_MAX_TABLE_COLUMNS", 3)
    book = openpyxl.Workbook()
    book.active.append(["a", "b", "c", "d"])
    book.save(str(tmp_path / "w.xlsx"))
    outcome = _read(tmp_path / "w.xlsx", "xlsx")
    assert outcome.truncated is True
    assert "| a | b | c |" in outcome.text and "d" not in outcome.text.split("\n", 2)[2]


def test_xlsx_narrow_sheet_is_not_marked_truncated(tmp_path):
    path = make_xlsx(tmp_path / "n.xlsx", [("S", [("Item", "Cost"), ("Rent", 1200)])])
    assert _read(path, "xlsx").truncated is False


# =============================================================================
# An encrypted PDF's page count comes from its page tree, never its /Count (review A, m)
# =============================================================================

@pytest.mark.parametrize("lie", [b"/Count 9\n", b"/Count -1", b"/Count 0\n"])
def test_owner_password_pdf_page_count_ignores_a_lying_count(tmp_path, lie):
    plain = make_pdf(tmp_path / "a.pdf", ["first page", "second page"])
    restricted = encrypt_pdf(plain, tmp_path / "restricted.pdf", user_password="").read_bytes()
    assert restricted.count(b"/Count 2\n") == 1
    lying = tmp_path / "lying.pdf"
    lying.write_bytes(restricted.replace(b"/Count 2\n", lie))
    outcome = _read(lying, "pdf")
    assert outcome.status is ExtractionStatus.TEXT
    assert outcome.pages_total == 2


# =============================================================================
# Short legacy texts: a decoding that mixes scripts in a word or inverts letter case loses the tie (review A, m)
# =============================================================================

@pytest.mark.parametrize("text,encoding", [
    ("Résumé", "cp1252"),                               # cp1251 reads it as "Rйsumй"
    ("Оплата 500 грн", "koi8_u"),                       # cp1251 reads it as "пРМБФБ 500 ЗТО"
    ("Привет, как дела?", "koi8_r"),
])
def test_very_short_legacy_texts_decode_to_plausible_words(text, encoding):
    assert decode_text(text.encode(encoding)) == text


# =============================================================================
# The PDF text engine is pdfminer.six, called directly: markitdown 0.1.8's PdfConverter goes through pdfplumber
# first and falls back on any exception (review A, M pins)
# =============================================================================

def test_pdf_text_comes_from_pdfminer_not_markitdowns_pdf_converter(tmp_path, monkeypatch):
    from markitdown import converters

    def refuse(*args, **kwargs):
        raise AssertionError("markitdown's PdfConverter must not read drive PDFs")

    monkeypatch.setattr(converters.PdfConverter, "convert", refuse)
    outcome = _read(make_pdf(tmp_path / "a.pdf", ["Invoice INV-2041 total 1250.00 EUR"]), "pdf")
    assert outcome.status is ExtractionStatus.TEXT
    assert "Invoice INV-2041" in outcome.text


def test_a_memory_failure_inside_pdfminer_is_not_swallowed(tmp_path, monkeypatch):
    # pdfminer is driven page by page (R2 P-m6): the failure is injected where it parses the page tree; a failure
    # while a page is laid out is pinned in test_drive_parser_pdf_text_cap.py
    from pdfminer.pdfpage import PDFPage

    def exhausted(*args, **kwargs):
        raise MemoryError("pdfminer")

    monkeypatch.setattr(PDFPage, "get_pages", exhausted)
    with pytest.raises(MemoryError):
        _read(make_pdf(tmp_path / "a.pdf", ["text"]), "pdf")

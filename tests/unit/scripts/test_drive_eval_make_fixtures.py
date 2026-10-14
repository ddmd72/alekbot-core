"""The fixture generator's text formats are byte-deterministic and readable by the libraries the parser uses
(USER_DRIVE_INDEXING_RFC §24.3: defusedxml for ODF content.xml, striprtf for RTF). The rendered formats are checked
on the committed files (tests/unit/fixtures/test_drive_eval_fixture_files.py)."""
import csv
import io
import zipfile

from defusedxml import ElementTree
from striprtf.striprtf import rtf_to_text

from scripts.validation.drive_eval import fixture_texts as T
from scripts.validation.drive_eval.make_fixtures import csv_bytes, odp_bytes, ods_bytes, odt_bytes, rtf_bytes


def _content_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.namelist()[0] == "mimetype" and zf.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        root = ElementTree.fromstring(zf.read("content.xml"))
    return " ".join(t for t in root.itertext())


def test_odf_formats_are_deterministic_and_readable():
    odt = odt_bytes(T.AKT_PARAGRAPHS)
    assert odt == odt_bytes(T.AKT_PARAGRAPHS)
    assert "7 300,00 грн" in _content_text(odt)
    ods = ods_bytes([("Category", "EUR"), *T.BUDGET_2026])
    assert ods == ods_bytes([("Category", "EUR"), *T.BUDGET_2026])
    assert "Groceries" in _content_text(ods) and "4200" in _content_text(ods)
    odp = odp_bytes(T.ROADMAP_SLIDES)
    assert "EUR 48,000" in _content_text(odp)


def test_rtf_escapes_cyrillic_and_reads_back():
    data = rtf_bytes(T.USTAV_PARAGRAPHS)
    assert data == rtf_bytes(T.USTAV_PARAGRAPHS) and data.isascii()
    assert "Членский взнос составляет 1 500 рублей в год" in rtf_to_text(data.decode("ascii"))


def test_csv_is_semicolon_separated():
    data = csv_bytes(("date", "category", "description", "amount_eur"), T.EXPENSES_CSV_ROWS)
    rows = list(csv.reader(io.StringIO(data.decode("utf-8")), delimiter=";"))
    assert rows[0] == ["date", "category", "description", "amount_eur"]
    assert ["2025-05-14", "Health", "Dentist (cleaning)", "85,00"] in rows


def test_the_dogovor_family_differs_only_where_the_gold_says():
    texts = {v: " ".join(T.dogovor_paragraphs(v)) for v in ("1", "2", "3")}
    assert {v: T.DOGOVOR_VERSIONS[v]["amount"] in texts[v] for v in texts} == {"1": True, "2": True, "3": True}
    assert "45 000" not in texts["1"] and "40 000" not in texts["2"]

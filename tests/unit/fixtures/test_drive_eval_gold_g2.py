"""The committed gold is true of the committed files (USER_DRIVE_INDEXING_RFC §17.8, §24.11):
- G1 and G2 together pass the runner's own validation against the tree (production request builder, R13);
- every G2 expected substring is in its file's text: the text layer read offline, or, for a file with no text layer
  (scans, screenshots, the broken-font PDF), the words the generator drew;
- the G2 set covers what §24.11 names: clauses, amounts, invoice numbers, dates, tables, two-column PDFs, a version
  family, the OCR-labelled cases, and every fixture language;
- OCR-labelled G1 cases find a file by subject only (no name key matches), so they measure the reader."""
import pytest
import io
import json
import zipfile
from pathlib import Path

from defusedxml import ElementTree
from striprtf.striprtf import rtf_to_text

from scripts.validation.drive_eval import fixture_texts as T
from scripts.validation.drive_eval.fixture_drive import FixtureDrive
from scripts.validation.drive_eval.metrics import expected_substrings, normalize_snippet
from scripts.validation.drive_eval.runner import validate_gold
from src.domain.drive_keys import name_keys_v2, name_match_v2, name_word_concepts, query_concepts
from src.domain.drive_tier_pool import concept_trigrams
from src.domain.drive_tiers import name_query_keys

_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "drive_eval"

# Files with no text layer: the words the generator drew (make_fixtures.FIXTURES).
_DRAWN = {
    "scan_0012.pdf": T.SCAN_ELECTRICITY_UK, "scan_0013.pdf": T.SCAN_HARDWARE_ES, "img_4912.jpg": T.RECEIPT_CAFE_RU,
    "screenshot_bank_en.png": T.SCREEN_BANK_EN, "screenshot_parcel_uk.png": T.SCREEN_PARCEL_UK,
    "screenshot_train_es.png": T.SCREEN_TRAIN_ES, "wa_dentist_ru.jpg": T.SCREEN_DENTIST_RU,
}


def _gold(name):
    return json.loads((_DIR / name).read_text(encoding="utf-8"))


def _content_of():
    return {i["path"]: i["content"] for i in json.loads((_DIR / "tree.json").read_text(encoding="utf-8"))["items"]
            if i.get("content")}


def _source_text(relative):
    name = Path(relative).name
    data = (_DIR / relative).read_bytes()
    if name in _DRAWN:
        return " ".join(text for _, text in _DRAWN[name])
    if name == "schet_faktura_1847.pdf":
        return T.SCHET_FAKTURA_HTML
    suffix = Path(name).suffix
    if suffix == ".pdf":
        from pdfminer.high_level import extract_text
        return extract_text(io.BytesIO(data))
    if suffix in {".docx", ".xlsx", ".pptx", ".html"}:
        from markitdown import MarkItDown
        return MarkItDown().convert_stream(io.BytesIO(data), file_extension=suffix).text_content
    if suffix in {".odt", ".ods", ".odp"}:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return " ".join(ElementTree.fromstring(zf.read("content.xml")).itertext())
    if suffix == ".rtf":
        return rtf_to_text(data.decode("ascii"))
    return data.decode("utf-8")


def test_the_gold_files_pass_the_runners_validation():
    drive = FixtureDrive.from_file(_DIR / "tree.json")
    validate_gold(_gold("gold_g1.json") + _gold("gold_g2.json"), drive.paths())


@pytest.mark.slow
def test_every_expected_substring_is_in_its_files_text():
    content = _content_of()
    wrong = []
    for case in _gold("gold_g2.json"):
        for path, text in expected_substrings(case["expected_paths"], case["expected_substring"]).items():
            if path.startswith("Archive/Invoices 2024/"):
                n = int(path.rsplit("-", 1)[-1].split(".")[0])
                source = T.INVOICE_2024_TEMPLATE.format(n=n)
            else:
                source = _source_text(content[path])
            if normalize_snippet(text) not in normalize_snippet(source):
                wrong.append((case["id"], path, text))
    assert wrong == []


def test_g2_covers_what_the_rfc_names():
    g2 = _gold("gold_g2.json")
    kinds = {c["case"] for c in g2}
    assert {"clause", "amount", "invoice_number", "date", "table", "two_column", "version_family"} <= kinds
    assert {c["lang"] for c in g2} >= {"en", "ru", "uk", "es"}
    ocr = [c for c in g2 if c.get("ocr")]
    assert len(ocr) >= 6 and {c["lang"] for c in ocr} >= {"en", "ru", "uk", "es"}
    assert all(c["goal"] == "G2" for c in g2) and len(g2) >= 25


def _query_name_keys(query):
    """The name keys the search queries with (v2: the v1 `query_name_keys` is retired, xc m3/m4)."""
    concepts = query_concepts(query).concepts
    if not concepts:           # xc F2: a query of ignored words only is searched by name, whole keys alone
        return [k for c in name_word_concepts(query) for k in name_query_keys(c)]
    return [k for c in concepts for k in (*name_query_keys(c), *concept_trigrams(c))]


def test_ocr_g1_cases_find_by_subject_only():
    ocr = [c for c in _gold("gold_g1.json") if c.get("ocr")]
    assert len(ocr) >= 4
    for case in ocr:
        for path in case["expected_paths"]:
            assert name_match_v2(name_keys_v2(path.rsplit("/", 1)[-1]), _query_name_keys(case["query"])) is None, case["id"]

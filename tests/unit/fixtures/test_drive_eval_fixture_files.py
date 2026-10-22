"""The eval's content files are what their cases say they are (USER_DRIVE_INDEXING_RFC §17.8, §24.11):
- every file the tree names exists, every committed file is named, and make_fixtures.py makes each one;
- every file the indexer would read has bytes, so no empty file reaches a parser or the reader;
- text-layer documents carry their gold text; scans carry none; the broken-font PDF extracts as (cid:N), which the
  §9 router reads as garbage; the bulk invoices are dense enough to stay with the parser;
- the files stay small and PII-free."""
import io
import json
import re
from pathlib import Path

import pytest

from scripts.validation.drive_eval.fixture_drive import FixtureDrive, item_id_for
from scripts.validation.drive_eval.make_fixtures import FIXTURES
from src.domain.drive_extraction import garbage_share
from src.domain.drive_index_params import MODEL_EXTRACT_DENSITY, MODEL_EXTRACT_GARBAGE
from src.domain.drive_index_policy import (
    SLICE2_CONTENT_KINDS, is_dot_name, is_generated_dir, is_marker_child, item_rule, kind_of,
)

pytestmark = pytest.mark.datafile

_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "drive_eval"
_FILES = _DIR / "files"
_MAX_TOTAL = 1_500_000


def _tree():
    return json.loads((_DIR / "tree.json").read_text(encoding="utf-8"))


def _named():
    return {i[k] for i in _tree()["items"] for k in ("content", "thumbnail") if i.get(k)}


def _pdf_text(name):
    from pdfminer.high_level import extract_text
    return extract_text(io.BytesIO((_FILES / name).read_bytes()))


def test_every_named_file_exists_and_every_file_is_named():
    on_disk = {f"files/{p.name}" for p in _FILES.iterdir() if p.is_file()}
    assert _named() == on_disk


def test_the_generator_makes_exactly_the_committed_files():
    assert {f"files/{name}" for name in FIXTURES} == _named()


def _readable_by_the_indexer(path):
    """Admitted by kind, and not inside a subtree the policy never stores or never reads (§5.1, §5.2)."""
    parts = path.split("/")
    folders, name = parts[:-1], parts[-1]
    if any(is_dot_name(f) or is_generated_dir(f) for f in folders):
        return False
    tree_paths = {i["path"] for i in _tree()["items"]}
    for depth in range(1, len(parts)):
        prefix = "/".join(parts[:depth]) + "/"
        if any(p.startswith(prefix) and "/" not in p[len(prefix):] and is_marker_child(p[len(prefix):])
               for p in tree_paths):
            return False        # a repository or a virtualenv: one opaque card
    return kind_of(name, "", False) in SLICE2_CONTENT_KINDS


@pytest.mark.slow
def test_every_file_the_indexer_reads_has_bytes():
    drive = FixtureDrive.from_file(_DIR / "tree.json")
    missing = []
    for path in drive.paths():
        change = drive._items[item_id_for(path)]
        if change.is_folder or item_rule(change) is not None or not _readable_by_the_indexer(path):
            continue
        if not drive.has_content(path):
            missing.append(path)
    assert missing == []


@pytest.mark.parametrize("name, phrases", [
    ("contrato_alquiler_2026.pdf", ["950,00 euros", "doce (12) meses"]),
    ("lease_porto.pdf", ["sixty (60) days", "EUR 1,250.00"]),
    ("super_important_agreement.pdf", ["five (5) years"]),
    ("tax_summary_2025.pdf", ["3,218.40"]),
    ("resume.pdf", ["Camille Exemple"]),
])
def test_text_layer_pdfs_carry_their_gold_text(name, phrases):
    text = " ".join(_pdf_text(name).split())
    assert [p for p in phrases if p not in text] == []


@pytest.mark.parametrize("name", ["scan_0012.pdf", "scan_0013.pdf"])
def test_scans_have_no_text_layer(name):
    assert _pdf_text(name).strip() == ""


def test_the_broken_font_pdf_extracts_as_garbage_the_router_sends_to_the_reader():
    text = _pdf_text("schet_faktura_1847.pdf")
    assert "(cid:" in text and "Итого" not in text
    assert garbage_share(text) > MODEL_EXTRACT_GARBAGE


def test_the_bulk_invoices_stay_with_the_parser():
    drive = FixtureDrive.from_file(_DIR / "tree.json")
    data = drive._content[item_id_for("Archive/Invoices 2024/invoice-417.pdf")]
    from pdfminer.high_level import extract_text
    text = extract_text(io.BytesIO(data))
    assert "INV-2024-417" in text and len(text.strip()) >= MODEL_EXTRACT_DENSITY   # one page, above the density floor


@pytest.mark.parametrize("name, magic", [
    ("img_4912.jpg", b"\xff\xd8\xff"), ("wa_dentist_ru.jpg", b"\xff\xd8\xff"), ("photo_rome.jpg", b"\xff\xd8\xff"),
    ("screenshot_bank_en.png", b"\x89PNG"), ("screenshot_parcel_uk.png", b"\x89PNG"),
    ("screenshot_train_es.png", b"\x89PNG"),
])
def test_pictures_are_jpeg_or_png(name, magic):
    assert (_FILES / name).read_bytes().startswith(magic)


def test_the_files_stay_small():
    assert sum(p.stat().st_size for p in _FILES.iterdir()) < _MAX_TOTAL


def test_only_reserved_example_addresses():
    """RFC 2606 example.org addresses are the only e-mail addresses in the fixture text formats."""
    for path in _FILES.iterdir():
        if path.suffix in {".eml", ".md", ".csv", ".html", ".vtt", ".rtf"}:
            found = re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", path.read_text(encoding="utf-8", errors="replace"))
            assert all(a.endswith("@example.org") for a in found), path.name

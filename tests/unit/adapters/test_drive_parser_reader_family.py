"""U5 frozen core: every outcome of `read_content` names the reader that produced it, and the child reports that family
with the PARSER_VERSION it ran (A-U5-2). The family table is tied to the readers' table and the domain's closed set.
Every file here is written in the test with invented content."""
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from src.adapters.drive_parser import readers
from src.adapters.drive_parser.child import dispatch
from src.adapters.drive_parser.readers import READER_FAMILY_OF_KIND, read_content
from src.domain.document_extraction import PARSER_VERSION, READER_FAMILIES, ExtractionStatus

_ROOT = Path(__file__).resolve().parents[3]
_BOOK = {
    "mimetype": "application/epub+zip",
    "META-INF/container.xml": (
        '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
        '</rootfiles></container>'),
    "OEBPS/content.opf": (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Example Library Guide</dc:title>'
        '<dc:identifier id="id">x</dc:identifier></metadata>'
        '<manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="c1"/></spine></package>'),
    "OEBPS/c1.xhtml": ('<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Opening hours</h1>'
                       '<p>The Example Library opens at nine.</p></body></html>'),
}


def _tar_package(path) -> str:
    """An Apple Books package: a plain tar of the EPUB folder."""
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as archive:
        for name, text in _BOOK.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return str(path)


def test_the_family_table_covers_every_reader_and_the_closed_set():
    assert set(READER_FAMILY_OF_KIND) == set(readers._READERS)
    assert set(READER_FAMILY_OF_KIND.values()) | {"epub_package", "text_html"} == READER_FAMILIES


@pytest.mark.parametrize("kind", sorted(READER_FAMILY_OF_KIND))
def test_each_kind_s_reader_reports_its_family(kind, tmp_path, monkeypatch):
    # the reader itself is replaced: this pins the routing from kind to family, the real readers are below
    monkeypatch.setitem(readers._READERS, kind, lambda path, k: ("Acme invoice FA-0137 for Alex Doe", 0, False))
    monkeypatch.setattr(readers, "_check_container", lambda path, k: None)
    path = tmp_path / "file.bin"
    path.write_bytes(b"plain bytes, not a tar")
    out = read_content(str(path), kind)
    assert out.status is ExtractionStatus.TEXT and out.reader_family == READER_FAMILY_OF_KIND[kind]


def _file(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return str(path)


@pytest.mark.parametrize("name, kind, data, family", [
    ("notes.txt", "text", "Shopping list for 12 Oak Street: bread, tea.", "text"),
    ("notes.md", "markdown", "# Garden meeting\n\nKim and Jordan bring the seeds.", "text"),
    ("page.md", "markdown", "<!DOCTYPE html><html><body><main><p>Example School parents' meeting on March 3."
                            "</p></main></body></html>", "text_html"),
    ("page.html", "html", "<html><body><main><p>Example Library opening hours.</p></main></body></html>", "html"),
    ("budget.csv", "csv", "item,amount\nrent,11400\ngroceries,4200\n", "csv"),
    ("meeting.vtt", "subtitles", "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nKim: the shed permit arrived.\n",
     "subtitles"),
    ("rent.eml", "eml", "From: landlord@example.org\nTo: tenant@example.org\nSubject: Rent\n\nRent is 950 EUR.\n",
     "eml"),
])
def test_real_readers_report_their_family(tmp_path, name, kind, data, family):
    out = read_content(_file(tmp_path, name, data), kind)
    assert out.status is ExtractionStatus.TEXT and out.reader_family == family


def test_a_text_file_that_is_not_html_stays_text(tmp_path):
    assert read_content(_file(tmp_path, "a.txt", "Plain words about the Skoda service."), "text").reader_family == "text"


def test_an_epub_tar_package_reports_epub_package(tmp_path):
    out = read_content(_tar_package(tmp_path / "book.epub"), "epub")
    assert out.status is ExtractionStatus.TEXT and out.reader_family == "epub_package"


def test_a_failed_read_still_names_the_reader_that_failed(tmp_path):
    out = read_content(_file(tmp_path, "broken.pdf", b"%PDF-1.4 not really a pdf"), "pdf")
    assert out.status is not ExtractionStatus.TEXT and out.reader_family == "pdf"


def test_a_kind_without_a_reader_has_no_family(tmp_path):
    out = read_content(_file(tmp_path, "x.bin", b"\x00\x01"), "image")
    assert out.status is ExtractionStatus.UNSUPPORTED and out.reader_family == ""


def test_dispatch_reports_the_version_and_the_family(tmp_path):
    result = dispatch({"op": "extract", "path": _file(tmp_path, "b.csv", "item,amount\nkindle,89\n"), "kind": "csv"})
    assert result["parser_version"] == PARSER_VERSION and result["reader_family"] == "csv"


def test_page_ops_carry_no_report(tmp_path):
    result = dispatch({"op": "pdf_page_count", "path": _file(tmp_path, "x.pdf", b"not a pdf")})
    assert "parser_version" not in result and "reader_family" not in result


def test_the_real_child_writes_both_keys(tmp_path):
    source = _file(tmp_path, "rent.eml", "From: landlord@example.org\nSubject: Rent\n\nRent is 950 EUR.\n")
    request = {"out": str(tmp_path / "result.json"), "memory_bytes": 2 * 1024 ** 3, "cpu_s": 30,
               "file_bytes": 64 * 1024 ** 2, "op": "extract", "path": source, "kind": "eml"}
    proc = subprocess.run([sys.executable, "-B", "-E", "-s", "-m", "src.adapters.drive_parser.child"],
                          input=json.dumps(request).encode(), cwd=_ROOT, capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert (result["parser_version"], result["reader_family"]) == (PARSER_VERSION, "eml")

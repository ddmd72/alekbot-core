"""D13's active kinds (EXTERNAL_FILES_RFC §4.2; owner decision O2): one case per kind from the bytes, a renamed file, a
kind seen only by its name, the container's macro facts, and the fail-closed rule when the macro check did not run."""
import pytest

from src.domain import active_kind as ak
from src.domain.active_kind import active_kind, needs_container_check
from src.domain.container_facts import ContainerFacts, ContainerStatus

PLAIN_ZIP = ContainerFacts(status=ContainerStatus.ZIP, members=9)
NOT_ZIP = ContainerFacts(status=ContainerStatus.NOT_ZIP)
LNK = b"\x4c\x00\x00\x00\x01\x14\x02\x00\x00\x00\x00\x00\xc0\x00\x00\x00\x00\x00\x00\x46" + b"\x00" * 60


def _iso(mark: bytes = b"CD001", offset: int = 32769) -> bytes:
    head = bytearray(40_000)
    head[offset:offset + len(mark)] = mark
    return bytes(head)


# -- one case per kind, from the bytes, under an innocent name -------------------------------------------------------

@pytest.mark.parametrize("head, kind", [
    (b"<!DOCTYPE html><html><body>Invoice FA-0137</body></html>", ak.HTML),
    (b"\xef\xbb\xbf\n  <html><head><title>Invoice</title></head></html>", ak.HTML),
    (b"<div>pay here</div><script>location='https://example.com'</script>", ak.HTML),
    ("<html>invoice</html>".encode("utf-16"), ak.HTML),
    (b"<html><head><hta:application id='x'/></head></html>", ak.HTA),
    (b"<?xml version='1.0'?><svg xmlns='http://www.w3.org/2000/svg'><script/></svg>", ak.SVG),
    (LNK, ak.SHORTCUT),
    (_iso(), ak.DISK_IMAGE),
    (_iso(b"NSR02"), ak.DISK_IMAGE),
    (b"MZ\x90\x00\x03\x00\x00\x00", ak.EXECUTABLE),
    (b"\x7fELF\x02\x01\x01\x00", ak.EXECUTABLE),
    (b"\xcf\xfa\xed\xfe\x07\x00\x00\x01", ak.EXECUTABLE),
    (b"#!/bin/sh\necho hello\n", ak.SCRIPT),
])
def test_the_bytes_name_the_kind_whatever_the_name_says(head, kind):
    # a renamed file: the name is a harmless kind, the bytes decide
    assert active_kind("invoice_bathroom_es.txt", "text/plain", head, None) == kind


def test_an_office_document_with_macros_is_seen_by_its_container_facts_under_a_plain_name():
    macro = ContainerFacts(status=ContainerStatus.ZIP, members=12, vba_project=True)
    assert active_kind("invoice_bathroom_es.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                       b"PK\x03\x04rest", macro) == ak.OFFICE_MACROS


@pytest.mark.parametrize("facts", [
    ContainerFacts(status=ContainerStatus.ZIP, members=3, macro_content_type=True),
    ContainerFacts(status=ContainerStatus.ZIP, members=3, odf_macros=True),
])
def test_each_container_macro_fact_blocks(facts):
    assert active_kind("plan.odt", "", b"PK\x03\x04", facts) == ak.OFFICE_MACROS


# -- the kinds without magic: the name, and a name or mime that blocks what the bytes would let through ---------------

@pytest.mark.parametrize("name, kind", [
    ("garage_rules.html", ak.HTML), ("rules.mhtml", ak.HTML), ("app.hta", ak.HTA), ("logo.svg", ak.SVG),
    ("invoice_bathroom_es.xlsm", ak.OFFICE_MACROS), ("letter.docm", ak.OFFICE_MACROS), ("slides.pptm", ak.OFFICE_MACROS),
    ("Invoice FA-0137.lnk", ak.SHORTCUT), ("backup.img", ak.DISK_IMAGE), ("install.iso", ak.DISK_IMAGE),
    ("setup.exe", ak.EXECUTABLE), ("invoice.pdf.exe", ak.EXECUTABLE), ("SETUP.EXE. ", ak.EXECUTABLE),
    ("run.js", ak.SCRIPT), ("macro.vbs", ak.SCRIPT), ("fix.ps1", ak.SCRIPT), ("start.bat", ak.SCRIPT),
    ("start.cmd", ak.SCRIPT),
])
def test_a_name_alone_blocks(name, kind):
    assert active_kind(name, "application/octet-stream", b"plain words, no magic", NOT_ZIP) == kind


@pytest.mark.parametrize("mime, kind", [
    ("text/html; charset=utf-8", ak.HTML), ("image/svg+xml", ak.SVG),
    ("application/vnd.ms-excel.sheet.macroEnabled.12", ak.OFFICE_MACROS),
    ("application/x-msdownload", ak.EXECUTABLE), ("application/javascript", ak.SCRIPT),
])
def test_a_declared_mime_alone_blocks(mime, kind):
    assert active_kind("attachment", mime, b"plain words", NOT_ZIP) == kind


def test_a_harmless_name_never_unblocks_active_bytes():
    assert active_kind("photo_roof.png", "image/png", b"MZ\x90\x00", None) == ak.EXECUTABLE


# -- what is sent --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name, mime, head, facts", [
    ("invoice_FA-0137.pdf", "application/pdf", b"%PDF-1.7\n", None),
    ("photo_roof.png", "image/png", b"\x89PNG\r\n\x1a\n", None),
    ("gym_pass.txt", "text/plain", b"Iron Temple gym pass, open 7-22", None),
    ("invoice.xml", "application/xml", b"<?xml version='1.0'?><Invoice><Total>249,00</Total></Invoice>", None),
    ("invoice_bathroom_es.docx", "", b"PK\x03\x04", PLAIN_ZIP),
    ("photos.zip", "application/zip", b"PK\x03\x04", PLAIN_ZIP),
])
def test_ordinary_files_are_not_active(name, mime, head, facts):
    assert active_kind(name, mime, head, facts) is None


# -- fail closed ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("facts", [
    None,
    ContainerFacts(status=ContainerStatus.ERROR, detail="timeout: deadline"),
    ContainerFacts(status=ContainerStatus.TOO_LARGE, members=10_001),
])
def test_a_container_that_was_not_checked_is_unchecked(facts):
    assert active_kind("invoice_bathroom_es.docx", "", b"PK\x03\x04", facts) == ak.UNCHECKED
    assert active_kind("invoice_bathroom_es.pdf", "application/pdf", b"PK\x03\x04", facts) == ak.UNCHECKED   # a ZIP head


def test_a_docx_name_on_bytes_that_are_not_a_zip_is_checked_and_sent():
    assert active_kind("invoice_bathroom_es.docx", "", b"plain text", NOT_ZIP) is None


@pytest.mark.parametrize("name, mime, head, needed", [
    ("a.bin", "", b"PK\x03\x04", True),
    ("a.bin", "", b"PK\x05\x06", True),
    ("plan.docx", "", b"", True),
    ("plan.ODT", "", b"", True),
    ("data", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", b"", True),
    ("data", "application/zip", b"", True),
    ("invoice.pdf", "application/pdf", b"%PDF-", False),
    ("notes.txt", "text/plain", b"Gym pass", False),
])
def test_needs_container_check(name, mime, head, needed):
    assert needs_container_check(name, mime, head) is needed


def test_only_the_head_is_read():
    tail_magic = b"x" * ak.ACTIVE_HEAD_BYTES + b"<html>"
    assert active_kind("notes.txt", "text/plain", tail_magic, None) is None


def test_every_kind_has_a_label():
    kinds = {ak.HTA, ak.HTML, ak.SVG, ak.OFFICE_MACROS, ak.SHORTCUT, ak.DISK_IMAGE, ak.EXECUTABLE, ak.SCRIPT, ak.UNCHECKED}
    assert set(ak.ACTIVE_KIND_LABELS) == kinds and all(ak.ACTIVE_KIND_LABELS.values())

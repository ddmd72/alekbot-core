"""Apple Books packages (`.epub.pkgf`, USER_DRIVE_INDEXING_RFC §5.1): kind `epub` from the name, admitted for content,
and a plain tar head keeps the declared EPUB kind so the parser repacks it."""
import pytest

from src.domain.drive_index import ContentClass, DriveChange, RecordState, Scope
from src.domain.drive_index_planner import classify
from src.domain.drive_index_policy import Admission, admission, content_class_of, kind_of
from src.domain.drive_sniff import sniff_kind


def _tar_head(name: bytes = b"mimetype") -> bytes:
    return name.ljust(257, b"\x00") + b"ustar\x0000" + b"\x00" * 246


@pytest.mark.parametrize("name", ["A Sample Book.epub.pkgf", "A Sample Book.EPUB.PKGF", "book.Epub.pkgf"])
def test_epub_package_is_an_epub(name):
    assert kind_of(name, "", is_folder=False) == "epub"
    assert kind_of(name, "application/octet-stream", is_folder=False) == "epub"


@pytest.mark.parametrize("name,kind", [
    ("book.pkgf", "other"), ("report.pages.pkgf", "other"), ("book.pkgf.epub", "epub"), ("epub.pkgf", "other"),
    ("book.epub", "epub"), (".epub.pkgf", "other"),
])
def test_only_an_epub_package_drops_its_pkgf_suffix(name, kind):
    assert kind_of(name, "", is_folder=False) == kind


def test_epub_package_is_admitted_as_a_document():
    kind = kind_of("A Sample Book.epub.pkgf", "", is_folder=False)
    assert admission(kind) is Admission.CONTENT
    assert content_class_of(kind) is ContentClass.DOCUMENT


def test_epub_package_is_planned_for_a_content_read():
    change = DriveChange(item_id="a", name="A Sample Book.epub.pkgf", parent_id="root", size_bytes=1000)
    cls = classify(change, Scope.NORMAL, None, None)
    assert (cls.kind, cls.state, cls.reason) == ("epub", RecordState.PENDING, None)


def test_tar_head_declared_epub_stays_epub():
    assert sniff_kind(_tar_head(), "epub") == "epub"


@pytest.mark.parametrize("declared", ["other", "text", "docx", "archive"])
def test_tar_head_under_another_kind_is_an_archive(declared):
    assert sniff_kind(_tar_head(), declared) == "archive"


def test_compressed_tar_declared_epub_is_an_archive():
    assert sniff_kind(b"\x1f\x8b\x08\x00" + b"\x00" * 600, "epub") == "archive"

"""Apple Books packages (`.epub.pkgf`, USER_DRIVE_INDEXING_RFC §5.1): a tar of the EPUB folder, repacked in memory by
the parser and read on the EPUB path. Every tar is built here with invented members."""
import io
import os
import tarfile
import zipfile

import pytest

from src.adapters.drive_parser import epub_package
from src.adapters.drive_parser.epub_package import PackageError, epub_zip_from_tar, is_tar_package
from src.adapters.drive_parser.readers import read_content
from src.domain.drive_extraction import ExtractionStatus

CONTAINER = ('<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
             '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
             '</rootfiles></container>')
OPF = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
       '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>A Sample Book</dc:title>'
       '<dc:identifier id="id">x</dc:identifier></metadata>'
       '<manifest><item id="c1" href="chapter1.xhtml" media-type="application/xhtml+xml"/></manifest>'
       '<spine><itemref idref="c1"/></spine></package>')
CHAPTER = ('<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Chapter One</h1>'
           '<p>The lighthouse keeper counted the ships.</p></body></html>')
BOOK = {"META-INF/container.xml": CONTAINER, "OEBPS/content.opf": OPF, "OEBPS/chapter1.xhtml": CHAPTER,
        "mimetype": "application/epub+zip"}


def _file(name, data):
    info = tarfile.TarInfo(name)
    payload = data.encode() if isinstance(data, str) else data
    info.size = len(payload)
    return info, io.BytesIO(payload)


def _special(name, kind, target=""):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    return info, None


def make_package(path, members, *, fmt=tarfile.PAX_FORMAT):
    """A plain tar of `members`: (TarInfo, data or None) pairs, or a name -> text mapping."""
    if isinstance(members, dict):
        members = [_file(name, data) for name, data in members.items()]
    with tarfile.open(path, "w", format=fmt) as archive:
        for info, data in members:
            archive.addfile(info, data)
    return str(path)


def _names(buffer):
    with zipfile.ZipFile(buffer) as archive:
        return archive.namelist()


# =============================================================================
# The package is read as an EPUB
# =============================================================================

@pytest.mark.parametrize("fmt", [tarfile.PAX_FORMAT, tarfile.USTAR_FORMAT, tarfile.GNU_FORMAT])
def test_tar_packaged_epub_is_read_as_an_epub(tmp_path, fmt):
    path = make_package(tmp_path / "book.epub.pkgf", BOOK, fmt=fmt)
    outcome = read_content(path, "epub")
    assert outcome.status is ExtractionStatus.TEXT
    assert "A Sample Book" in outcome.text
    assert "The lighthouse keeper counted the ships." in outcome.text


def test_package_tarred_with_its_folder_is_read_from_that_folder(tmp_path):
    path = make_package(tmp_path / "book.epub.pkgf", {f"A Sample Book.epub/{n}": d for n, d in BOOK.items()})
    outcome = read_content(path, "epub")
    assert outcome.status is ExtractionStatus.TEXT
    assert "The lighthouse keeper counted the ships." in outcome.text


def test_dot_slash_names_are_normalised(tmp_path):
    path = make_package(tmp_path / "book.epub.pkgf", {f"./{n}": d for n, d in BOOK.items()})
    assert set(_names(epub_zip_from_tar(path))) == set(BOOK)
    assert read_content(path, "epub").status is ExtractionStatus.TEXT


def test_repacked_zip_is_stored_with_mimetype_first(tmp_path):
    path = make_package(tmp_path / "book.epub.pkgf", BOOK)          # mimetype is the last tar member
    with zipfile.ZipFile(epub_zip_from_tar(path)) as archive:
        infos = archive.infolist()
        assert infos[0].filename == "mimetype"
        assert {info.compress_type for info in infos} == {zipfile.ZIP_STORED}
        assert archive.read("OEBPS/chapter1.xhtml").decode() == CHAPTER


def test_repack_writes_nothing_to_disk(tmp_path):
    path = make_package(tmp_path / "book.epub.pkgf", BOOK)
    before = sorted(os.listdir(tmp_path))
    read_content(path, "epub")
    assert sorted(os.listdir(tmp_path)) == before


def test_drm_inside_a_package_is_encrypted(tmp_path):
    path = make_package(tmp_path / "book.epub.pkgf", {**BOOK, "META-INF/rights.xml": "<rights/>"})
    assert read_content(path, "epub").status is ExtractionStatus.ENCRYPTED


def test_a_zip_epub_takes_the_zip_path(tmp_path):
    path = tmp_path / "book.epub.pkgf"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in BOOK.items():
            archive.writestr(name, data)
    assert not is_tar_package(str(path))
    assert read_content(str(path), "epub").status is ExtractionStatus.TEXT


# =============================================================================
# Untrusted members
# =============================================================================

@pytest.mark.parametrize("member", [
    _special("OEBPS/link.xhtml", tarfile.SYMTYPE, "/etc/passwd"),
    _special("OEBPS/hard.xhtml", tarfile.LNKTYPE, "OEBPS/chapter1.xhtml"),
    _special("OEBPS/fifo", tarfile.FIFOTYPE),
    _special("OEBPS/dev", tarfile.CHRTYPE),
    _special("OEBPS/images", tarfile.DIRTYPE),
    _file("../outside.xhtml", "<p>escaped</p>"),
    _file("OEBPS/../../outside.xhtml", "<p>escaped</p>"),
    _file("/absolute.xhtml", "<p>absolute</p>"),
    _file("OEBPS/._chapter1.xhtml", b"\x00\x05\x16\x07"),
], ids=["symlink", "hardlink", "fifo", "device", "directory", "dotdot", "inner-dotdot", "absolute", "appledouble"])
def test_unsafe_members_are_skipped(tmp_path, member):
    members = [_file(name, data) for name, data in BOOK.items()] + [member]
    path = make_package(tmp_path / "book.epub.pkgf", members)
    assert set(_names(epub_zip_from_tar(path))) == set(BOOK)
    outcome = read_content(path, "epub")
    assert outcome.status is ExtractionStatus.TEXT
    assert "escaped" not in outcome.text and "absolute" not in outcome.text


def test_repeated_name_keeps_the_first_copy(tmp_path):
    members = [_file(name, data) for name, data in BOOK.items()] + [_file("OEBPS/chapter1.xhtml", "<p>second</p>")]
    path = make_package(tmp_path / "book.epub.pkgf", members)
    with zipfile.ZipFile(epub_zip_from_tar(path)) as archive:
        assert archive.namelist().count("OEBPS/chapter1.xhtml") == 1
        assert archive.read("OEBPS/chapter1.xhtml").decode() == CHAPTER


def test_folder_prefix_is_kept_when_members_sit_under_several_folders(tmp_path):
    path = make_package(tmp_path / "book.epub.pkgf", {"one/a.xhtml": "<p>a</p>", "two/b.xhtml": "<p>b</p>"})
    assert set(_names(epub_zip_from_tar(path))) == {"one/a.xhtml", "two/b.xhtml"}


def test_folder_prefix_is_kept_beside_a_root_member(tmp_path):
    path = make_package(tmp_path / "book.epub.pkgf", {"mimetype": "application/epub+zip", "OEBPS/a.xhtml": "<p>a</p>"})
    assert set(_names(epub_zip_from_tar(path))) == {"mimetype", "OEBPS/a.xhtml"}


def test_an_empty_package_gets_the_outcome_of_an_empty_epub_zip(tmp_path):
    path = make_package(tmp_path / "book.epub.pkgf", [_special("OEBPS", tarfile.DIRTYPE)])
    assert _names(epub_zip_from_tar(path)) == []
    zipped = tmp_path / "empty.epub"
    with zipfile.ZipFile(zipped, "w"):
        pass
    assert read_content(path, "epub").status is read_content(str(zipped), "epub").status   # no container.xml either way


# =============================================================================
# Bounds and broken packages: the outcomes an EPUB ZIP gets
# =============================================================================

def test_member_count_over_the_bound_is_too_large(tmp_path, monkeypatch):
    monkeypatch.setattr(epub_package, "MAX_ZIP_MEMBERS", 5)
    members = [_file(name, data) for name, data in BOOK.items()] + [_special(f"d{i}", tarfile.DIRTYPE) for i in range(2)]
    path = make_package(tmp_path / "book.epub.pkgf", members)        # 6 headers: directories count too
    outcome = read_content(path, "epub")
    assert outcome.status is ExtractionStatus.TOO_LARGE


def test_member_count_at_the_bound_is_read(tmp_path, monkeypatch):
    monkeypatch.setattr(epub_package, "MAX_ZIP_MEMBERS", len(BOOK))
    path = make_package(tmp_path / "book.epub.pkgf", BOOK)
    assert read_content(path, "epub").status is ExtractionStatus.TEXT


def test_member_size_over_the_bound_is_too_large(tmp_path, monkeypatch):
    total = sum(len(data.encode()) for data in BOOK.values())
    monkeypatch.setattr(epub_package, "MAX_ZIP_UNCOMPRESSED_BYTES", total - 1)
    path = make_package(tmp_path / "book.epub.pkgf", BOOK)
    assert read_content(path, "epub").status is ExtractionStatus.TOO_LARGE


def test_member_size_at_the_bound_is_read(tmp_path, monkeypatch):
    total = sum(len(data.encode()) for data in BOOK.values())
    monkeypatch.setattr(epub_package, "MAX_ZIP_UNCOMPRESSED_BYTES", total)
    path = make_package(tmp_path / "book.epub.pkgf", BOOK)
    assert read_content(path, "epub").status is ExtractionStatus.TEXT


def test_a_declared_size_is_bounded_before_any_member_is_read(tmp_path, monkeypatch):
    path = make_package(tmp_path / "book.epub.pkgf", BOOK)
    monkeypatch.setattr(epub_package, "MAX_ZIP_UNCOMPRESSED_BYTES", 10)
    read = []
    monkeypatch.setattr(tarfile.TarFile, "extractfile", lambda self, member: read.append(member) or None)
    with pytest.raises(epub_package.ArchiveTooLargeError):
        epub_zip_from_tar(path)
    assert read == []


def test_not_a_tar_is_the_corrupt_epub_outcome(tmp_path):
    path = tmp_path / "book.epub.pkgf"
    path.write_bytes(b"this is neither a tar nor a zip" * 40)
    assert not is_tar_package(str(path))
    corrupt = tmp_path / "corrupt.epub"
    corrupt.write_bytes(b"this is neither a tar nor a zip" * 40)
    outcome = read_content(str(path), "epub")
    assert outcome.status is ExtractionStatus.UNSUPPORTED
    assert outcome.status is read_content(str(corrupt), "epub").status


def test_a_truncated_package_is_unsupported(tmp_path):
    full = make_package(tmp_path / "full.tar", {"OEBPS/big.xhtml": "<p>page</p>" * 500, **BOOK})
    data = open(full, "rb").read()
    path = tmp_path / "book.epub.pkgf"
    path.write_bytes(data[:512 + 200])                 # the first header and part of its data
    assert is_tar_package(str(path))
    with pytest.raises(PackageError):
        epub_zip_from_tar(str(path))
    assert read_content(str(path), "epub").status is ExtractionStatus.UNSUPPORTED


def test_short_file_is_not_a_tar_package(tmp_path):
    path = tmp_path / "book.epub.pkgf"
    path.write_bytes(b"ustar")
    assert not is_tar_package(str(path))


def test_member_data_shorter_than_its_header_is_a_package_error(tmp_path, monkeypatch):
    path = make_package(tmp_path / "book.epub.pkgf", BOOK)
    monkeypatch.setattr(tarfile.TarFile, "extractfile", lambda self, member: io.BytesIO(b"x"))
    with pytest.raises(PackageError, match="ends early"):
        epub_zip_from_tar(path)

"""Domain helpers for saving bot-written text to the drive (USER_DRIVE_RFC §4.7.1)."""
import pytest

from src.domain.user_drive import (
    CREATABLE_TEXT_MIME_TYPES,
    MAX_DRIVE_TEXT_FILE_BYTES,
    TEXT_FILE_EXTENSIONS,
    DrivePathError,
    prepare_drive_text_file,
)


def test_limit_is_five_mib():
    assert MAX_DRIVE_TEXT_FILE_BYTES == 5 * 1024 * 1024


def test_creatable_extensions_per_rfc():
    assert set(CREATABLE_TEXT_MIME_TYPES) == {
        ".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".html", ".svg", ".xml"}


def test_append_extension_list_unchanged():
    assert TEXT_FILE_EXTENSIONS == (".md", ".txt", ".csv", ".json", ".yaml", ".yml")


@pytest.mark.parametrize("name,mime", [
    ("a.md", "text/markdown"), ("a.txt", "text/plain"), ("a.csv", "text/csv"),
    ("a.json", "application/json"), ("a.svg", "image/svg+xml"), ("a.HTML", "text/html"),
])
def test_mime_from_extension(name, mime):
    filename, _, got = prepare_drive_text_file(name, "x")
    assert got == mime and filename == name


def test_utf8_no_bom_and_lf_endings():
    _, data, _ = prepare_drive_text_file("n.md", "Привіт\r\nрядок\rкінець\n")
    assert data == "Привіт\nрядок\nкінець\n".encode("utf-8")
    assert not data.startswith(b"\xef\xbb\xbf")


def test_text_otherwise_untouched():
    _, data, _ = prepare_drive_text_file("n.txt", "  spaced \n\n")
    assert data == b"  spaced \n\n"


def test_name_sanitized_keeps_extension():
    filename, _, _ = prepare_drive_text_file("a/b:c.md", "x")
    assert filename == "a_b_c.md"


@pytest.mark.parametrize("name", ["pic.png", "doc.pdf", "report.docx", "noext"])
def test_disallowed_name_refused_with_hint(name):
    with pytest.raises(DrivePathError) as exc:
        prepare_drive_text_file(name, "x")
    assert "file_ref" in str(exc.value)


def test_size_limit_on_encoded_bytes():
    prepare_drive_text_file("a.txt", "a" * MAX_DRIVE_TEXT_FILE_BYTES)
    with pytest.raises(DrivePathError):
        prepare_drive_text_file("a.txt", "a" * (MAX_DRIVE_TEXT_FILE_BYTES + 1))
    # multi-byte: 3M Cyrillic chars = 6 MB encoded
    with pytest.raises(DrivePathError):
        prepare_drive_text_file("a.txt", "я" * 3_000_000)


@pytest.mark.parametrize("name", ["", "   ", None])
def test_missing_name_refused(name):
    with pytest.raises(DrivePathError) as exc:
        prepare_drive_text_file(name, "x")
    assert "name" in str(exc.value)

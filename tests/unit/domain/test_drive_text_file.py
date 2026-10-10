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


def test_leading_bom_dropped_only_once():
    """§4.7.1 "UTF-8 without BOM": a leading U+FEFF from the model would encode as a BOM."""
    _, data, _ = prepare_drive_text_file("n.md", "\ufeff\ufeffx")
    assert data == "\ufeffx".encode("utf-8")   # the second one is content, stored as given


def test_lone_surrogate_is_a_clean_refusal():
    with pytest.raises(DrivePathError) as exc:
        prepare_drive_text_file("n.md", "ok \ud800 bad")
    assert "position 3" in str(exc.value)


def test_extension_case_insensitive_name_kept():
    filename, _, mime = prepare_drive_text_file("Звіт.MD", "x")
    assert filename == "Звіт.MD" and mime == "text/markdown"


@pytest.mark.parametrize("name,ok", [("x.md.png", False), ("x.png.md", True), ("../x.md", True)])
def test_last_extension_decides(name, ok):
    if ok:
        filename, _, _ = prepare_drive_text_file(name, "x")
        assert "/" not in filename and filename.endswith(".md")
    else:
        with pytest.raises(DrivePathError):
            prepare_drive_text_file(name, "x")


def test_extension_without_stem_refused_with_hint():
    with pytest.raises(DrivePathError) as exc:
        prepare_drive_text_file(".md", "x")
    assert "notes.md" in str(exc.value)

"""
A ZIP container is checked on its central directory before any converter opens it (the converter allowlist is
tests/unit/utils/test_markitdown_allowlist.py).
"""

import zipfile

import pytest

from src.utils.file_conversion import (
    ArchiveTooLargeError,
    MAX_ZIP_MEMBERS,
    MAX_ZIP_UNCOMPRESSED_BYTES,
    check_zip_container,
)

# =============================================================================
# check_zip_container — central-directory caps
# =============================================================================

def test_check_zip_container_ignores_non_zip(tmp_path):
    f = tmp_path / "notes.bin"
    f.write_bytes(b"not an archive")
    check_zip_container(str(f))  # no exception


def test_check_zip_container_accepts_small_archive(tmp_path):
    f = tmp_path / "ok.zip"
    with zipfile.ZipFile(f, "w") as z:
        z.writestr("a.txt", "hello")
    check_zip_container(str(f))


def test_check_zip_container_rejects_inflated_size(tmp_path):
    f = tmp_path / "bomb.docx"
    chunk = b"\0" * (1024 * 1024)
    with zipfile.ZipFile(f, "w", compression=zipfile.ZIP_DEFLATED) as z:
        with z.open("word/document.xml", "w", force_zip64=True) as member:
            for _ in range(MAX_ZIP_UNCOMPRESSED_BYTES // len(chunk) + 1):
                member.write(chunk)
    assert f.stat().st_size < 5 * 1024 * 1024  # passes the upload cap — that is the attack
    with pytest.raises(ArchiveTooLargeError):
        check_zip_container(str(f))


@pytest.mark.slow
def test_check_zip_container_rejects_too_many_members(tmp_path):
    f = tmp_path / "many.xlsx"
    with zipfile.ZipFile(f, "w") as z:
        for i in range(MAX_ZIP_MEMBERS + 1):
            z.writestr(f"m{i}", b"")
    with pytest.raises(ArchiveTooLargeError):
        check_zip_container(str(f))

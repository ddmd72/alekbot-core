"""src/utils/document_hardening.py: the Phase 0 hardening in a module the drive parser's child imports per file, so
it must not pull the logger and telemetry (USER_DRIVE_INDEXING_RFC §9)."""
import subprocess
import sys
from pathlib import Path

from src.utils import document_hardening, file_conversion

_ROOT = Path(__file__).resolve().parents[3]


def test_file_conversion_re_exports_the_same_objects():
    assert file_conversion.build_markitdown is document_hardening.build_markitdown
    assert file_conversion.check_zip_container is document_hardening.check_zip_container
    assert file_conversion.ArchiveTooLargeError is document_hardening.ArchiveTooLargeError
    assert file_conversion.MAX_ZIP_MEMBERS == document_hardening.MAX_ZIP_MEMBERS
    assert file_conversion.MAX_ZIP_UNCOMPRESSED_BYTES == document_hardening.MAX_ZIP_UNCOMPRESSED_BYTES


def test_importing_it_loads_neither_the_logger_nor_telemetry_nor_markitdown():
    code = ("import sys, src.utils.document_hardening\n"
            "print(sorted(m for m in ('src.utils.logger', 'src.utils.telemetry', 'markitdown') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], cwd=_ROOT, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"

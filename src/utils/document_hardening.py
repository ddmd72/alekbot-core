"""
Hardening for parsers that read untrusted user files (chat uploads, drive files, email attachments).

Kept free of the logger and telemetry imports so the drive extraction child process
(`src/adapters/drive_parser/child.py`) starts in milliseconds, not seconds: it imports this module
under its memory limit for every file (USER_DRIVE_INDEXING_RFC §9). `file_conversion` re-exports it.
"""

import zipfile
from typing import Any

# OOXML / EPUB are ZIP containers: a 5 MB upload can declare gigabytes once inflated. Checked on
# the archive's central directory before any converter opens it.
MAX_ZIP_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_ZIP_MEMBERS = 10_000


class ArchiveTooLargeError(ValueError):
    """A ZIP container declares more inflated bytes or members than the caps allow."""


def check_zip_container(local_path: str) -> None:
    """Refuse a ZIP container (docx/xlsx/pptx/epub/…) whose central directory declares too much.

    Reads only the directory, never inflates a member. Not a ZIP → no-op.
    """
    if not zipfile.is_zipfile(local_path):
        return
    with zipfile.ZipFile(local_path) as archive:
        infos = archive.infolist()
    if len(infos) > MAX_ZIP_MEMBERS:
        raise ArchiveTooLargeError(f"{len(infos)} members (limit {MAX_ZIP_MEMBERS})")
    declared = sum(info.file_size for info in infos)
    if declared > MAX_ZIP_UNCOMPRESSED_BYTES:
        raise ArchiveTooLargeError(f"{declared} bytes inflated (limit {MAX_ZIP_UNCOMPRESSED_BYTES})")


def build_markitdown() -> Any:
    """MarkItDown with local document converters only — the files are untrusted user input.

    Builtins stay off: ZipConverter inflates every archive member in RAM (decompression bomb),
    AudioConverter uploads audio to Google's speech API, Image/URL converters call out to
    exiftool and the network. Registration order mirrors `MarkItDown.enable_builtins`.
    """
    from markitdown import MarkItDown, PRIORITY_GENERIC_FILE_FORMAT  # lazy — optional dependency
    from markitdown.converters import (
        CsvConverter, DocxConverter, EpubConverter, HtmlConverter, IpynbConverter,
        OutlookMsgConverter, PdfConverter, PlainTextConverter, PptxConverter, RssConverter,
        XlsConverter, XlsxConverter,
    )

    md = MarkItDown(enable_builtins=False, enable_plugins=False)
    md.register_converter(PlainTextConverter(), priority=PRIORITY_GENERIC_FILE_FORMAT)
    md.register_converter(HtmlConverter(), priority=PRIORITY_GENERIC_FILE_FORMAT)
    for converter in (
        RssConverter(), DocxConverter(), XlsxConverter(), XlsConverter(), PptxConverter(),
        IpynbConverter(), PdfConverter(), OutlookMsgConverter(), EpubConverter(), CsvConverter(),
    ):
        md.register_converter(converter)
    return md

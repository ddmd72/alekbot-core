"""
Hardening for parsers that read untrusted user files (chat uploads, drive files, email attachments).

Kept free of the logger and telemetry imports so the drive extraction child process
(`src/adapters/drive_parser/child.py`) starts in milliseconds, not seconds: it imports this module
under its memory limit for every file (USER_DRIVE_INDEXING_RFC §9). `file_conversion` re-exports it.
"""

import posixpath
import re
import zipfile
import zlib
from typing import Any, Dict

# OOXML / EPUB are ZIP containers: a 5 MB upload can declare gigabytes once inflated. Checked on
# the archive's central directory before any converter opens it.
MAX_ZIP_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_ZIP_MEMBERS = 10_000
# `[Content_Types].xml` of a real OOXML file is a few KiB; the one member `zip_container_facts` inflates, read up to this.
MAX_CONTENT_TYPES_BYTES = 1024 * 1024
# A macro-enabled OOXML main part (`…macroEnabled.main+xml`, `…macroEnabled.12`) or a VBA project part type.
_MACRO_CONTENT_TYPE = re.compile(rb"macroenabled|vbaproject", re.IGNORECASE)
# ODF keeps Basic libraries under `Basic/` and other script languages under `Scripts/`; a document without macros has
# neither folder.
_ODF_MACRO_DIRS = ("Basic/", "Scripts/")


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


def zip_container_facts(local_path: str) -> Dict[str, Any]:
    """The facts of a ZIP container that EXTERNAL_FILES D13 needs (owner decision O2: run in the parser child of the
    sandbox's `parse.container_facts` op, never in the main service). The result is the op's body
    (`sandbox_parse_result.CONTAINER_RESULT_KEYS`):

        {"status": "not_zip"}                                   no ZIP central directory
        {"status": "too_large", "members": n, "detail": …}      above MAX_ZIP_MEMBERS, or a content-types part above
                                                                MAX_CONTENT_TYPES_BYTES: the facts are not given
        {"status": "error", "detail": …}                        a ZIP the reader cannot read
        {"status": "zip", "members": n, "vba_project": bool, "macro_content_type": bool, "odf_macros": bool}

    Reads the central directory and inflates one member only, `[Content_Types].xml`, bounded. A member named
    `vbaProject.bin` anywhere (OOXML keeps it under `word/`, `xl/`, `ppt/`), a content type naming a macro-enabled part
    or a VBA project, and an ODF `Basic/` or `Scripts/` folder are the three macro facts."""
    if not zipfile.is_zipfile(local_path):
        return {"status": "not_zip"}
    try:
        with zipfile.ZipFile(local_path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_MEMBERS:
                return {"status": "too_large", "members": len(infos),
                        "detail": f"{len(infos)} members (limit {MAX_ZIP_MEMBERS})"}
            names = [info.filename for info in infos]
            content_types = next((info for info in infos if info.filename == "[Content_Types].xml"), None)
            macro_content_type = False
            if content_types is not None:
                if content_types.file_size > MAX_CONTENT_TYPES_BYTES:
                    return {"status": "too_large", "members": len(infos), "detail": "content types part too large"}
                with archive.open(content_types) as handle:
                    data = handle.read(MAX_CONTENT_TYPES_BYTES + 1)
                if len(data) > MAX_CONTENT_TYPES_BYTES:
                    return {"status": "too_large", "members": len(infos), "detail": "content types part too large"}
                macro_content_type = _MACRO_CONTENT_TYPE.search(data) is not None
    except (zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, NotImplementedError, RuntimeError, ValueError,
            OSError, EOFError) as exc:   # RuntimeError: an encrypted member; NotImplementedError: an unknown compression
        return {"status": "error", "detail": type(exc).__name__}
    return {
        "status": "zip",
        "members": len(names),
        "vba_project": any(posixpath.basename(name).lower() == "vbaproject.bin" for name in names),
        "macro_content_type": macro_content_type,
        "odf_macros": any(name.startswith(_ODF_MACRO_DIRS) for name in names),
    }


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

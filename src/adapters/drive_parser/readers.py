"""
Per-kind text readers of the drive parser (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §5.1, §8.3, §9, §24.3).

They run only inside the sandboxed child process (`drive_parser.child`), never in the indexer process: every
parser here may amplify a small file into gigabytes (§9, Phase 0). Parser libraries are imported inside the
functions, so they load after the child has applied its limits.

Every admitted kind has a named reader (§5.1): markitdown's allowlisted converters (Phase 0,
`src/utils/document_hardening.build_markitdown`) for PDF, DOCX, PPTX, EPUB, MSG and HTML; charset detection for the
text kinds, VTT/SRT reduced to cue text; `striprtf` for RTF; the stdlib `email` parser for EML; ODF `content.xml`
through `defusedxml`; XLSX/XLS/CSV/ODS as Markdown tables capped at MAX_SHEET_ROWS rows per sheet (the chunker
repeats the header row, §8.3). Password-protected PDF/Office/EPUB/ODF → `encrypted`.

Public interface:
    MARKITDOWN_CONVERTERS
    read_content(path, kind) -> ExtractionOutcome          # `extracted_by` is stamped by the parent
    pdf_page_count(path) -> int                                   # raises PdfSplitFailed
    cut_pdf_window(path, first_page, page_count, out_path) -> int   # pages written; raises PdfSplitFailed
    decode_text(data, cut) -> str
    subtitle_cues(text) -> str
    markdown_table(rows) -> Tuple[str, bool]
"""
from __future__ import annotations

import codecs
import csv
import datetime as dt
import html
import io
import os
import re
import zipfile
from contextlib import contextmanager
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple
from typing import Iterator

from src.adapters.drive_parser.child import is_memory_failure
from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus, PdfSplitFailed
from src.domain.drive_index_params import MAX_SHEET_ROWS, MAX_TEXT_CHARS
from src.utils.document_hardening import ArchiveTooLargeError, check_zip_container

# kind -> markitdown converter class (all registered by build_markitdown; a test keeps the two in step). Called
# directly with the sniffed kind: no magika guess, no fall-through to another converter.
MARKITDOWN_CONVERTERS: Mapping[str, Tuple[str, str, str]] = {
    "pdf": ("PdfConverter", ".pdf", "application/pdf"),
    "docx": ("DocxConverter", ".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "pptx": ("PptxConverter", ".pptx",
             "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    "epub": ("EpubConverter", ".epub", "application/epub+zip"),
    "msg": ("OutlookMsgConverter", ".msg", "application/vnd.ms-outlook"),
}
_ZIP_KINDS: FrozenSet[str] = frozenset({"docx", "xlsx", "pptx", "epub", "odt", "ods", "odp"})
_OOXML_KINDS: FrozenSet[str] = frozenset({"docx", "xlsx", "pptx"})
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_FONT_SUFFIXES = (".ttf", ".otf", ".woff", ".woff2")   # EPUB font obfuscation is not DRM
_MAX_TABLE_COLUMNS = 1024    # bounds a hostile repeat count or dimension; real sheets stay far below
_CSV_SNIFF_CHARS = 4096
# Legacy code pages of the languages the bot serves (uk, ru, en, es, fr), in tie-break order.
_PREFERRED_LEGACY = ("cp1251", "cp1252", "cp1250", "koi8_u", "koi8_r", "iso8859_15", "latin_1")
_CHAOS_MARGIN = 0.1
_ODF = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
}
_Read = Tuple[str, int, bool]    # text, pages_total, truncated


class _Encrypted(Exception):
    """The file needs a password we do not have."""


class _WrongContainer(ValueError):
    """The bytes are not the container their kind needs (an OLE2 file under a ZIP kind)."""


# -- entry points ---------------------------------------------------------------------------------------------------

def read_content(path: str, kind: str) -> ExtractionOutcome:
    """Text of one file. A per-file failure is an outcome; a memory failure propagates as MemoryError so the child
    exits with its memory code (§8.2: `memory` is per-file transient, never `error`)."""
    reader = _READERS.get(kind)
    if reader is None:
        return ExtractionOutcome(status=ExtractionStatus.UNSUPPORTED, detail=f"no reader for kind {kind}")
    try:
        if kind in _ZIP_KINDS:
            _check_container(path, kind)
        text, pages_total, truncated = reader(path, kind)
    except ArchiveTooLargeError as exc:
        return ExtractionOutcome(status=ExtractionStatus.TOO_LARGE, detail=str(exc))
    except _Encrypted as exc:
        return ExtractionOutcome(status=ExtractionStatus.ENCRYPTED, detail=str(exc))
    except Exception as exc:  # any parser failure on hostile input is this file's outcome, never the tick's
        if is_memory_failure(exc):
            raise MemoryError(str(exc)) from exc
        status = ExtractionStatus.UNSUPPORTED if _deterministic(exc) else ExtractionStatus.ERROR
        return ExtractionOutcome(status=status, detail=f"{type(exc).__name__}: {exc}"[:300])
    return _finish(text, pages_total, truncated)


def pdf_page_count(path: str) -> int:
    """Pages of a PDF the reader may cut (§24.3). An owner-password-only PDF counts; a user password is
    `encrypted`."""
    with _pdf_failures():
        return len(_open_pdf(path).pages)


def cut_pdf_window(path: str, first_page: int, page_count: int, out_path: str) -> int:
    """Write pages [first_page, first_page + page_count) of the PDF (0-based, clamped to its end) as one window PDF
    at `out_path`; returns the pages written. One window per call: the reader cuts on demand, so at most
    MODEL_IN_FLIGHT windows exist on disk at once (§24.3) — a window can carry a resource every page shares."""
    with _pdf_failures():
        from pypdf import PdfWriter

        reader = _open_pdf(path)
        total = len(reader.pages)
        if first_page < 0 or page_count < 1 or first_page >= total:
            raise PdfSplitFailed(ExtractionStatus.ERROR, f"pages {first_page}+{page_count} outside 0..{total}")
        last = min(first_page + page_count, total)
        writer = PdfWriter()
        for index in range(first_page, last):
            writer.add_page(reader.pages[index])
        with open(out_path, "wb") as fh:
            writer.write(fh)
        return last - first_page


def _open_pdf(path: str) -> Any:
    from pypdf import PdfReader

    reader = PdfReader(path, strict=False)
    if reader.is_encrypted and not reader.decrypt(""):
        raise PdfSplitFailed(ExtractionStatus.ENCRYPTED, "user password")
    return reader


@contextmanager
def _pdf_failures() -> Iterator[None]:
    """A failure of the page-window ops as PdfSplitFailed with its outcome status; a memory failure propagates as
    MemoryError so the child exits with its memory code."""
    try:
        yield
    except PdfSplitFailed:
        raise
    except Exception as exc:  # any parser failure on hostile input is this file's outcome, never the tick's
        if is_memory_failure(exc):
            raise MemoryError(str(exc)) from exc
        status = ExtractionStatus.UNSUPPORTED if _deterministic(exc) else ExtractionStatus.ERROR
        raise PdfSplitFailed(status, f"{type(exc).__name__}: {exc}"[:300]) from exc


# -- shared helpers -------------------------------------------------------------------------------------------------

def decode_text(data: bytes, cut: bool = False) -> str:
    """BOM first, then strict UTF-8, then charset-normalizer's best guess (§5.1). `cut`: `data` is a prefix of the
    file, so a character split at its end is dropped instead of replaced."""
    for bom, encoding in ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF32_LE, "utf-32-le"),
                          (codecs.BOM_UTF32_BE, "utf-32-be"), (codecs.BOM_UTF16_LE, "utf-16-le"),
                          (codecs.BOM_UTF16_BE, "utf-16-be")):
        if data.startswith(bom):
            return _decode(data[len(bom):], encoding, cut, "replace")
    try:
        return _decode(data, "utf-8", cut, "strict")
    except UnicodeDecodeError:
        return _decode(data, _guess_encoding(data), cut, "replace")


def subtitle_cues(text: str) -> str:
    """VTT/SRT reduced to cue text (§8.1 step 6): header, NOTE/STYLE/REGION blocks, cue ids, timing lines and blank
    separators go; `<v Name>` becomes `Name: `; other tags go; a rolled-up repeat of the previous line goes."""
    lines = [line.strip() for line in text.splitlines()]
    out: List[str] = []
    skipping = False
    for index, line in enumerate(lines):
        if not line:
            skipping = False
            continue
        if skipping or "-->" in line:
            continue
        if line.startswith("WEBVTT") or re.match(r"(NOTE|STYLE|REGION)(\s|$)", line):
            skipping = True
            continue
        if index + 1 < len(lines) and "-->" in lines[index + 1]:
            continue                                  # a cue identifier (SRT number or VTT id)
        cue = re.sub(r"<v(?:\.[^\s>]+)?\s+([^>]+)>", r"\1: ", line)
        cue = html.unescape(re.sub(r"</?[^>]+>", "", cue)).strip()
        if cue and (not out or out[-1] != cue):
            out.append(cue)
    return "\n".join(out)


def markdown_table(rows: Sequence[Sequence[Any]]) -> Tuple[str, bool]:
    """A Markdown table, first row as header; empty rows and trailing empty columns dropped. Returns
    (table, truncated): rows past MAX_SHEET_ROWS and columns past the column cap are cut."""
    cells: List[List[str]] = []
    truncated = False
    for row in rows:
        values = [_cell(value) for value in row]
        if len(values) > _MAX_TABLE_COLUMNS:
            values, truncated = values[:_MAX_TABLE_COLUMNS], True
        if any(values):
            cells.append(values)
    if len(cells) > MAX_SHEET_ROWS:
        cells, truncated = cells[:MAX_SHEET_ROWS], True
    if not cells:
        return "", truncated
    width = max((max((i + 1 for i, v in enumerate(row) if v), default=0) for row in cells), default=0)
    padded = [(row + [""] * width)[:width] for row in cells]
    lines = ["| " + " | ".join(padded[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in padded[1:]]
    return "\n".join(lines), truncated


# -- readers --------------------------------------------------------------------------------------------------------

def _read_markitdown(path: str, kind: str, *, charset: Optional[str] = None) -> str:
    from markitdown import StreamInfo
    from markitdown import converters

    class_name, extension, mimetype = MARKITDOWN_CONVERTERS[kind]
    converter = getattr(converters, class_name)()
    info = StreamInfo(extension=extension, mimetype=mimetype, charset=charset, local_path=path,
                      filename=os.path.basename(path))
    with open(path, "rb") as fh:
        return str(converter.convert(fh, info).markdown or "")


def _read_pdf(path: str, kind: str) -> _Read:
    from pypdf import PdfReader

    pages_total = 0
    try:
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise _Encrypted("user password")
        pages_total = len(reader.pages)
    except _Encrypted:
        raise
    except Exception as exc:  # pypdf cannot count a damaged file pdfminer may still read; density then is unknown
        if is_memory_failure(exc):
            raise
        pages_total = 0
    return _read_markitdown(path, kind), pages_total, False


def _read_converter(path: str, kind: str) -> _Read:
    return _read_markitdown(path, kind), 0, False


def _read_html(path: str, kind: str) -> _Read:
    from markitdown.converters import HtmlConverter

    data, cut = _read_prefix(path)
    return str(HtmlConverter().convert_string(decode_text(data, cut)).markdown or ""), 0, cut


def _read_text(path: str, kind: str) -> _Read:
    data, cut = _read_prefix(path)
    return decode_text(data, cut), 0, cut


def _read_subtitles(path: str, kind: str) -> _Read:
    data, cut = _read_prefix(path)
    return subtitle_cues(decode_text(data, cut)), 0, cut


def _read_csv(path: str, kind: str) -> _Read:
    data, cut = _read_prefix(path)
    text = decode_text(data, cut)
    try:
        dialect: Any = csv.Sniffer().sniff(text[:_CSV_SNIFF_CHARS], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows: List[List[str]] = []
    try:
        for row in csv.reader(io.StringIO(text), dialect):
            rows.append(row)
            if len(rows) > MAX_SHEET_ROWS:
                break
    except csv.Error:
        return text, 0, cut                           # not parseable as a table: indexed as text
    table, truncated = markdown_table(rows)
    return table, 0, cut or truncated


def _read_rtf(path: str, kind: str) -> _Read:
    from striprtf.striprtf import rtf_to_text

    data, cut = _read_prefix(path)
    return str(rtf_to_text(data.decode("latin-1"), errors="replace")), 0, cut


def _read_eml(path: str, kind: str) -> _Read:
    import email
    import email.policy
    from email.message import EmailMessage

    with open(path, "rb") as fh:
        message = email.message_from_binary_file(fh, policy=email.policy.default)
    if not isinstance(message, EmailMessage):
        raise ValueError("not a MIME message")
    header = [f"{name}: {message[name]}" for name in ("Subject", "From", "To", "Cc", "Date") if message[name]]
    body = message.get_body(preferencelist=("plain", "html"))
    text = ""
    if isinstance(body, EmailMessage):
        content = _part_text(body)
        if body.get_content_subtype() == "html":
            from markitdown.converters import HtmlConverter

            content = str(HtmlConverter().convert_string(content).markdown or "")
        text = content
    return "\n".join(header + ["", text]).strip(), 0, False


def _read_xlsx(path: str, kind: str) -> _Read:
    import openpyxl

    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheets: List[Tuple[str, List[Sequence[Any]], bool]] = []
        for sheet in book.worksheets:
            sheet.reset_dimensions()                  # a declared A1:XFD1048576 must not pad every row
            rows: List[Sequence[Any]] = []
            for row in sheet.iter_rows(values_only=True):
                if any(value is not None and str(value).strip() for value in row):
                    rows.append(row)
                if len(rows) > MAX_SHEET_ROWS:
                    break
            sheets.append((sheet.title, rows, False))
    finally:
        book.close()
    return _sheets_text(sheets)


def _read_xls(path: str, kind: str) -> _Read:
    import xlrd

    try:
        book = xlrd.open_workbook(path, on_demand=True)
    except xlrd.XLRDError as exc:
        if "encrypted" in str(exc).lower():
            raise _Encrypted(str(exc)) from exc
        raise
    try:
        sheets: List[Tuple[str, List[Sequence[Any]], bool]] = []
        for index in range(book.nsheets):
            sheet = book.sheet_by_index(index)
            rows: List[Sequence[Any]] = []
            for r in range(min(sheet.nrows, MAX_SHEET_ROWS + 1)):
                rows.append([_xls_value(cell, book.datemode) for cell in sheet.row(r)])
            sheets.append((sheet.name, rows, sheet.nrows > MAX_SHEET_ROWS + 1))
            book.unload_sheet(index)
    finally:
        book.release_resources()
    return _sheets_text(sheets)


def _read_odf(path: str, kind: str) -> _Read:
    from defusedxml import ElementTree

    with zipfile.ZipFile(path) as archive:
        content = archive.read("content.xml")
    root = ElementTree.fromstring(content)
    body = root.find(f"{{{_ODF['office']}}}body")
    if body is None:
        return "", 0, False
    blocks: List[str] = []
    truncated = _odf_blocks(body, kind, blocks)
    return "\n\n".join(block for block in blocks if block.strip()), 0, truncated


_READERS: Dict[str, Callable[[str, str], _Read]] = {
    "pdf": _read_pdf, "docx": _read_converter, "pptx": _read_converter, "epub": _read_converter,
    "msg": _read_converter, "xlsx": _read_xlsx, "xls": _read_xls, "odt": _read_odf, "ods": _read_odf,
    "odp": _read_odf, "html": _read_html, "text": _read_text, "markdown": _read_text, "json": _read_text,
    "csv": _read_csv, "subtitles": _read_subtitles, "rtf": _read_rtf, "eml": _read_eml,
}


# -- internals ------------------------------------------------------------------------------------------------------

def _check_container(path: str, kind: str) -> None:
    """ZIP caps (Phase 0) and encryption, before any parser opens the package."""
    with open(path, "rb") as fh:
        magic = fh.read(len(_OLE_MAGIC))
    if magic == _OLE_MAGIC:
        if kind in _OOXML_KINDS and "EncryptedPackage" in _ole_streams(path):
            raise _Encrypted("encrypted OOXML package")
        raise _WrongContainer("an OLE2 file under a ZIP kind")
    check_zip_container(path)
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        if kind == "epub" and _epub_encrypted(archive, names):
            raise _Encrypted("EPUB DRM")
        if kind in ("odt", "ods", "odp") and _odf_encrypted(archive, names):
            raise _Encrypted("ODF password")


def _deterministic(exc: Exception) -> bool:
    """A failure the same bytes always produce: `unsupported` (terminal, §8.2), not `error` (retried MAX_ATTEMPTS
    times). Nesting too deep for the parser, a container that is not one, XML with a DTD or entities."""
    from defusedxml import DefusedXmlException

    return isinstance(exc, (RecursionError, zipfile.BadZipFile, _WrongContainer, DefusedXmlException))


def _ole_streams(path: str) -> FrozenSet[str]:
    import olefile

    if not olefile.isOleFile(path):
        return frozenset()
    with olefile.OleFileIO(path) as ole:
        return frozenset("/".join(entry) for entry in ole.listdir())


def _epub_encrypted(archive: zipfile.ZipFile, names: Iterable[str]) -> bool:
    names = set(names)
    if "META-INF/rights.xml" in names:
        return True
    if "META-INF/encryption.xml" not in names:
        return False
    from defusedxml import ElementTree

    root = ElementTree.fromstring(archive.read("META-INF/encryption.xml"))
    uris = [el.get("URI", "") for el in root.iter() if el.tag.endswith("CipherReference")]
    return any(not uri.lower().endswith(_FONT_SUFFIXES) for uri in uris)


def _odf_encrypted(archive: zipfile.ZipFile, names: Iterable[str]) -> bool:
    if "META-INF/manifest.xml" not in set(names):
        return False
    from defusedxml import ElementTree

    root = ElementTree.fromstring(archive.read("META-INF/manifest.xml"))
    return any(el.tag.endswith("}encryption-data") for el in root.iter())


def _odf_blocks(node: Any, kind: str, out: List[str]) -> bool:
    """Paragraphs, headings, tables and slides in document order; returns whether a table was cut."""
    truncated = False
    for child in node:
        tag = child.tag
        if tag in (_q("text", "p"), _q("text", "h")):
            line = _odf_inline(child).strip()
            if tag == _q("text", "h") and line:
                level = int(child.get(_q("text", "outline-level"), "1") or "1")
                line = "#" * max(1, min(level, 6)) + " " + line
            out.append(line)
        elif tag == _q("table", "table"):
            table, cut = markdown_table(_odf_rows(child))
            truncated = truncated or cut
            if kind == "ods":
                out.append(f"## {child.get(_q('table', 'name'), '')}".rstrip())
            out.append(table)
        elif tag == _q("draw", "page"):
            out.append(f"## {child.get(_q('draw', 'name'), '')}".rstrip())
            truncated = _odf_blocks(child, kind, out) or truncated
        else:
            truncated = _odf_blocks(child, kind, out) or truncated
    return truncated


def _odf_rows(table: Any) -> List[List[str]]:
    rows: List[List[str]] = []
    for row in table.iter(_q("table", "table-row")):
        cells: List[str] = []
        pending_empty = 0
        for cell in row:
            if cell.tag not in (_q("table", "table-cell"), _q("table", "covered-table-cell")):
                continue
            repeat = min(int(cell.get(_q("table", "number-columns-repeated"), "1") or "1"), _MAX_TABLE_COLUMNS + 1)
            value = " ".join(_odf_inline(p).strip() for p in cell.iter(_q("text", "p"))).strip()
            if not value:
                pending_empty += repeat              # trailing empties are never materialised
                continue
            cells.extend([""] * min(pending_empty, _MAX_TABLE_COLUMNS + 1))
            pending_empty = 0
            cells.extend([value] * repeat)
            if len(cells) > _MAX_TABLE_COLUMNS:
                break
        if not any(cells):
            continue                                 # empty rows, however often repeated, cost nothing
        repeat = min(int(row.get(_q("table", "number-rows-repeated"), "1") or "1"), MAX_SHEET_ROWS + 1)
        rows.extend([cells] * repeat)
        if len(rows) > MAX_SHEET_ROWS:
            break
    return rows


def _odf_inline(node: Any) -> str:
    parts = [node.text or ""]
    for child in node:
        if child.tag == _q("text", "s"):
            parts.append(" " * min(int(child.get(_q("text", "c"), "1") or "1"), 100))
        elif child.tag == _q("text", "tab"):
            parts.append("\t")
        elif child.tag == _q("text", "line-break"):
            parts.append("\n")
        elif child.tag != _q("text", "note"):
            parts.append(_odf_inline(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _q(namespace: str, name: str) -> str:
    return f"{{{_ODF[namespace]}}}{name}"


def _sheets_text(sheets: Sequence[Tuple[str, List[Sequence[Any]], bool]]) -> _Read:
    blocks: List[str] = []
    truncated = False
    for title, rows, cut in sheets:
        table, table_cut = markdown_table(rows)
        truncated = truncated or cut or table_cut
        if table:
            blocks.append(f"## {title}\n\n{table}")
    return "\n\n".join(blocks), 0, truncated


def _xls_value(cell: Any, datemode: int) -> Any:
    import xlrd

    if cell.ctype == xlrd.XL_CELL_DATE:
        try:
            return xlrd.xldate_as_datetime(cell.value, datemode)
        except (ValueError, OverflowError):
            return cell.value
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return bool(cell.value)
    return cell.value


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, dt.datetime):
        return value.date().isoformat() if value.time() == dt.time() else value.isoformat(sep=" ")
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    return " ".join(str(value).split()).replace("|", "\\|")


def _part_text(part: Any) -> str:
    try:
        return str(part.get_content())
    except (LookupError, UnicodeError, AttributeError, KeyError):
        payload = part.get_payload(decode=True) or b""
        return decode_text(payload) if isinstance(payload, bytes) else str(payload)


def _read_prefix(path: str) -> Tuple[bytes, bool]:
    """At most what MAX_TEXT_CHARS characters can take (4 bytes each); `cut` when the file is longer."""
    limit = MAX_TEXT_CHARS * 4
    with open(path, "rb") as fh:
        data = fh.read(limit + 1)
    return (data[:limit], True) if len(data) > limit else (data, False)


def _guess_encoding(data: bytes) -> str:
    """charset-normalizer's ranking, except that a common legacy code page whose chaos is within _CHAOS_MARGIN of
    the best wins: on a short file the detector ties CP1251 with mac_cyrillic or a CJK code page."""
    from charset_normalizer import from_bytes

    matches = list(from_bytes(data))
    if not matches:
        return "utf-8"
    best = matches[0]
    near = [m for m in matches if m.encoding in _PREFERRED_LEGACY and m.chaos <= best.chaos + _CHAOS_MARGIN]
    if not near:
        return str(best.encoding)
    return str(max(near, key=lambda m: (m.coherence, -_PREFERRED_LEGACY.index(m.encoding))).encoding)


def _decode(data: bytes, encoding: str, cut: bool, errors: str) -> str:
    decoder = codecs.getincrementaldecoder(encoding)(errors=errors)
    return str(decoder.decode(data, final=not cut))


def _finish(text: str, pages_total: int, truncated: bool) -> ExtractionOutcome:
    text = text.replace("\x00", "")
    text = "\n".join(line.rstrip() for line in re.split(r"\r?\n", text))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > MAX_TEXT_CHARS:
        text, truncated = text[:MAX_TEXT_CHARS], True
    if not text:
        return ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=pages_total, truncated=truncated)
    return ExtractionOutcome(status=ExtractionStatus.TEXT, text=text, pages_total=pages_total, truncated=truncated)

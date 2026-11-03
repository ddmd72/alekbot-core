"""
A document's kind from its name, its declared mime type and its first bytes (docs/10_rfcs/EXTERNAL_FILES_RFC.md
§4.5; the rules are docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §5.1, §5.5 class 2, §8.1 step 4 and GOOGLE_DRIVE_RFC §5).
Pure and source-neutral: the drive indexer, the drive open path and the document service all read kinds here.

Name and mime (`kind_of`): the name's extension decides, unless G1's mime table names a document kind the extension
does not (`mime_decides_kind`); an Apple Books package (`<name>.epub.pkgf`) reads as the extension it packs.

Head (`sniff_kind`): magic bytes win over the declared kind (the one `kind_of` gave from the name and mime type): a
`.txt` that is a ZIP is an archive, and the caller sends the sniffed kind back through the policy. A container whose
content cannot be told from the head (OOXML members past the head, an OLE2 directory sector elsewhere in the file)
keeps the declared kind when that kind can live in the container, else it is `archive` (ZIP) or `other` (OLE2).
Encrypted OOXML is an OLE2 file by the standard, so an OOXML kind stays: the parser reports it `encrypted`. A plain
tar declared EPUB stays EPUB: an Apple Books package (`.epub.pkgf`), which the parser repacks as an EPUB ZIP.

`image_mime` names the four image formats every vision provider of the reader and triage takes, from the first bytes
(a provider rendition's mime type does not describe it, spike S0f); anything else is "".

Public interface:
    SNIFF_HEAD_BYTES
    sniff_kind(head, declared_kind) -> str
    image_mime(head) -> str
    kind_of(name, mime, is_folder) -> str
    mime_kind(mime) -> Optional[str]
    mime_decides_kind(name, mime) -> bool
"""
from __future__ import annotations

import os
import struct
from typing import Dict, FrozenSet, List, Optional, Tuple

_EXT_KIND: Dict[str, str] = {
    ".pdf": "pdf", ".docx": "docx", ".doc": "doc", ".pptx": "pptx", ".ppt": "ppt", ".xlsx": "xlsx",
    ".xls": "xls", ".odt": "odt", ".ods": "ods", ".odp": "odp", ".epub": "epub", ".html": "html",
    ".htm": "html", ".xhtml": "html", ".mhtml": "mhtml", ".mht": "mhtml", ".md": "markdown",
    ".markdown": "markdown", ".txt": "text", ".csv": "csv", ".json": "json", ".rtf": "rtf", ".msg": "msg",
    ".eml": "eml", ".vtt": "subtitles", ".srt": "subtitles",
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image", ".webp": "image", ".heic": "image",
    ".heif": "image", ".tif": "image", ".tiff": "image", ".bmp": "image", ".svg": "image", ".raw": "image",
    ".dng": "image", ".mp3": "audio", ".m4a": "audio", ".wav": "audio", ".flac": "audio", ".ogg": "audio",
    ".opus": "audio", ".aac": "audio", ".mp4": "video", ".mov": "video", ".webm": "video", ".mkv": "video",
    ".avi": "video", ".zip": "archive", ".7z": "archive", ".rar": "archive", ".tar": "archive", ".gz": "archive",
    ".tgz": "archive", ".bz2": "archive", ".xz": "archive", ".py": "code", ".js": "code", ".ts": "code",
    ".tsx": "code", ".jsx": "code", ".java": "code", ".kt": "code", ".go": "code", ".rs": "code", ".c": "code",
    ".h": "code", ".cpp": "code", ".cs": "code", ".rb": "code", ".php": "code", ".swift": "code", ".sh": "code",
    ".yaml": "code", ".yml": "code", ".toml": "code", ".ini": "code", ".xml": "code", ".css": "code",
    ".ipynb": "code", ".log": "log", ".sql": "data_dump", ".har": "data_dump", ".jsonl": "data_dump",
    ".ndjson": "data_dump", ".sqlite": "database", ".db": "database", ".mdb": "database", ".accdb": "database",
    ".exe": "executable", ".dll": "executable", ".msi": "executable", ".dmg": "disk_image", ".iso": "disk_image",
    ".img": "disk_image", ".vhd": "disk_image", ".pages": "iwork", ".numbers": "iwork", ".key": "iwork",
}
# An Apple Books package (`<name>.epub.pkgf`) is a tar of the EPUB folder; the parser repacks it (§5.1).
_PACKAGE_EXT = ".pkgf"
_PACKAGED_EXTS: FrozenSet[str] = frozenset({".epub"})
# G1 (GOOGLE_DRIVE_RFC §5): the document mimes the parser reads. IANA names only, never a provider's own type.
_MIME_KIND: Dict[str, str] = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/pdf": "pdf",
    "text/markdown": "markdown",
    "text/plain": "text",
    "text/csv": "csv",
}
_MIME_TEXT_KINDS: FrozenSet[str] = frozenset({"markdown", "text", "csv"})
_MIME_TABLE_KINDS: FrozenSet[str] = frozenset(_MIME_KIND.values())

SNIFF_HEAD_BYTES = 8192     # what the caller reads from the temp file; local ZIP headers and OLE2 names fit often

_OOXML_KINDS: FrozenSet[str] = frozenset({"docx", "xlsx", "pptx"})
_ODF_KINDS: FrozenSet[str] = frozenset({"epub", "odt", "ods", "odp"})     # a `mimetype` member names the kind
_OLE_KINDS: FrozenSet[str] = frozenset({"xls", "msg", "doc", "ppt"}) | _OOXML_KINDS   # OOXML: encrypted package
_TEXT_KINDS: FrozenSet[str] = frozenset({"text", "markdown", "csv", "json", "subtitles", "html", "eml", "code",
                                         "log", "data_dump", "mhtml"})

_ODF_MIME = {
    b"application/epub+zip": "epub",
    b"application/vnd.oasis.opendocument.text": "odt",
    b"application/vnd.oasis.opendocument.spreadsheet": "ods",
    b"application/vnd.oasis.opendocument.presentation": "odp",
}
_OOXML_PARTS = (("word/", "docx"), ("xl/", "xlsx"), ("ppt/", "pptx"))
# UTF-16LE directory entry names of an OLE2 compound file, when the directory sector is inside the head.
_OLE_NAMES = (
    ("EncryptedPackage", ""),          # encrypted OOXML: the declared kind stays
    ("__substg1.0_", "msg"), ("__properties_version1.0", "msg"),
    ("Workbook", "xls"), ("Book", "xls"),
    ("WordDocument", "doc"), ("PowerPoint Document", "ppt"),
)
_SIGNATURES: Tuple[Tuple[int, bytes, str], ...] = (
    (0, b"\x89PNG\r\n\x1a\n", "image"), (0, b"\xff\xd8\xff", "image"), (0, b"GIF87a", "image"),
    (0, b"GIF89a", "image"), (0, b"II*\x00", "image"), (0, b"MM\x00*", "image"),
    (0, b"\x1f\x8b", "archive"), (0, b"7z\xbc\xaf\x27\x1c", "archive"), (0, b"Rar!\x1a\x07", "archive"),
    (0, b"\xfd7zXZ\x00", "archive"), (257, b"ustar", "archive"),
    (0, b"\x7fELF", "executable"), (0, b"\xcf\xfa\xed\xfe", "executable"),
    (0, b"SQLite format 3\x00", "database"), (0, b"fLaC", "audio"), (0, b"OggS", "audio"),
    (0, b"\x1aE\xdf\xa3", "video"),
)
# Printable magics a text file could begin with: they count only in a binary head.
_WEAK_SIGNATURES: Tuple[Tuple[bytes, str], ...] = ((b"MZ", "executable"), (b"BZh", "archive"), (b"ID3", "audio"))
_FTYP_IMAGE = (b"heic", b"heix", b"heim", b"heis", b"hevc", b"mif1", b"msf1", b"avif")
_FTYP_AUDIO = (b"M4A ", b"M4B ")
_BOMS = (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")


def sniff_kind(head: bytes, declared_kind: str) -> str:
    """The kind the head proves, else the declared kind when the head is compatible with it, else `other`."""
    if head.startswith(b"%PDF-"):
        return "pdf"
    if declared_kind in ("pdf", "other") and b"%PDF-" in head[:1024]:
        return "pdf"                                 # up to 1 KiB of junk before the header; a text naming it is text
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return _zip_kind(head, declared_kind)
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return _ole_kind(head, declared_kind)
    if head.startswith(b"{\\rtf"):
        return "rtf"
    riff = _riff_kind(head)
    if riff:
        return riff
    ftyp = _ftyp_kind(head)
    if ftyp:
        return ftyp
    if declared_kind == "epub" and head[257:262] == b"ustar":
        return "epub"                                # an Apple Books package (`.epub.pkgf`): the parser repacks it
    for offset, magic, kind in _SIGNATURES:
        if head[offset:offset + len(magic)] == magic:
            return kind
    for magic, kind in _WEAK_SIGNATURES:
        if head.startswith(magic) and b"\x00" in head:
            return kind
    if declared_kind == "image":
        return "image"                               # SVG and the rarer raw formats: images are never parsed
    if declared_kind in _TEXT_KINDS and _looks_like_text(head):
        return _html_or(head, declared_kind)
    return "other"


def _zip_kind(head: bytes, declared_kind: str) -> str:
    """The head proves a kind only by a readable `mimetype` value (ODF, EPUB) or an OOXML part name. Otherwise the
    declared ZIP kind stays (members may sit past the head, a streamed ZIP stops the walk at its first member, a
    `mimetype` may be deflated or late), unless the head shows the other family's marker: `mimetype` under an OOXML
    kind, `[Content_Types].xml` under an ODF/EPUB kind."""
    names = _zip_local_names(head)
    for name, stored in names:
        if name == "mimetype" and stored is not None:
            return _ODF_MIME.get(stored.strip(), "archive")
        for prefix, kind in _OOXML_PARTS:
            if name.startswith(prefix):
                return kind
    visible = {name for name, _ in names}
    if declared_kind in _OOXML_KINDS and "mimetype" not in visible:
        return declared_kind
    if declared_kind in _ODF_KINDS and "[Content_Types].xml" not in visible:
        return declared_kind
    if declared_kind == "iwork":
        return "iwork"
    return "archive"


def _zip_local_names(head: bytes) -> List[Tuple[str, Optional[bytes]]]:
    """(name, stored data or None) of the local file headers inside the head, in order."""
    found: List[Tuple[str, Optional[bytes]]] = []
    pos = 0
    while head[pos:pos + 4] == b"PK\x03\x04" and pos + 30 <= len(head):
        flags, method = struct.unpack_from("<HH", head, pos + 6)
        csize = struct.unpack_from("<I", head, pos + 18)[0]
        nlen, xlen = struct.unpack_from("<HH", head, pos + 26)
        start = pos + 30
        if start + nlen > len(head):
            break
        name = head[start:start + nlen].decode("utf-8", errors="replace")
        data_at = start + nlen + xlen
        streamed = bool(flags & 0x08) or csize == 0xFFFFFFFF   # data descriptor or ZIP64: sizes are not here
        readable = method == 0 and not streamed and data_at + csize <= len(head)
        found.append((name, head[data_at:data_at + csize] if readable else None))
        if streamed:                                 # the next header is not locatable
            break
        pos = data_at + csize
    return found


def _ole_kind(head: bytes, declared_kind: str) -> str:
    for name, kind in _OLE_NAMES:
        if name.encode("utf-16-le") in head:
            if not kind:
                return declared_kind if declared_kind in _OOXML_KINDS else "other"
            return kind
    return declared_kind if declared_kind in _OLE_KINDS else "other"


def _riff_kind(head: bytes) -> str:
    if not head.startswith(b"RIFF"):
        return ""
    return {b"WEBP": "image", b"WAVE": "audio", b"AVI ": "video"}.get(head[8:12], "other")


def _ftyp_kind(head: bytes) -> str:
    if head[4:8] != b"ftyp":
        return ""
    brand = head[8:12]
    if brand in _FTYP_IMAGE:
        return "image"
    if brand in _FTYP_AUDIO:
        return "audio"
    return "video"


def _looks_like_text(head: bytes) -> bool:
    if head.startswith(_BOMS):
        return True
    return b"\x00" not in head


def _html_or(head: bytes, declared_kind: str) -> str:
    """A text file whose content is an HTML document is converted as HTML (§5.1: never read raw)."""
    if declared_kind not in ("text", "markdown"):
        return declared_kind
    start = head.lstrip(b"\xef\xbb\xbf \t\r\n")[:64].lower()
    if start.startswith((b"<!doctype html", b"<html")):
        return "html"
    return declared_kind


def image_mime(head: bytes) -> str:
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(head) >= 12 and head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    return ""


def mime_kind(mime: str) -> Optional[str]:
    """The document kind `mime` names (parameters such as `; charset=utf-8` ignored, case-insensitive), else None."""
    return _MIME_KIND.get(mime.split(";", 1)[0].strip().lower())


def _effective_ext(name: str) -> str:
    """The name's extension, lower-cased, with an Apple Books package unwrapped to the extension it packs."""
    stem, ext = os.path.splitext(name.lower())
    if ext == _PACKAGE_EXT and os.path.splitext(stem)[1] in _PACKAGED_EXTS:
        ext = os.path.splitext(stem)[1]
    return ext


def _kind_from_ext(name: str) -> Optional[str]:
    return _EXT_KIND.get(_effective_ext(name))


def mime_decides_kind(name: str, mime: str) -> bool:
    """True when `mime` decides the kind instead of the name's extension (G1, design Δ4): the mime names a table kind
    and the extension gives no kind, or the extension's kind is another table kind and the two are not both text
    kinds. An extension whose kind is outside the table (log, code, subtitles, json, …) always keeps its reading."""
    by_mime = mime_kind(mime)
    if by_mime is None:
        return False
    by_ext = _kind_from_ext(name)
    if by_ext is None:
        return True
    if by_ext == by_mime or by_ext not in _MIME_TABLE_KINDS:
        return False
    return not (by_ext in _MIME_TEXT_KINDS and by_mime in _MIME_TEXT_KINDS)


def kind_of(name: str, mime: str, is_folder: bool) -> str:
    if is_folder:
        return "folder"
    ext = _effective_ext(name)
    kind = _kind_from_ext(name)
    if kind == "iwork" and ext == ".key" and mime not in ("", "application/vnd.apple.keynote"):
        return "other"   # a .key that is not Keynote is a secret file, caught by item_rule first
    by_mime = mime_kind(mime) if mime_decides_kind(name, mime) else None
    if by_mime:
        return by_mime
    if kind:
        return kind
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    return "other"

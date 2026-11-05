"""
D13: the "active" kinds `send_file_to_user` does not forward from an email attachment (docs/10_rfcs/EXTERNAL_FILES_RFC.md
§4.2 "Active kinds from email are not sent", D13; owner decision O2 of 2026-11-05). Pure, stdlib only.

The whole decision is one function over four inputs: the attachment's name, its declared mime type, the head of its
bytes (`ACTIVE_HEAD_BYTES`, enough for the ISO marker at 32 769) and the container facts the sandbox read
(`container_facts.ContainerFacts`, from `parse.container_facts`; None when no check ran). The head checks are byte
compares (a header's classification, not parsing); the only structural read, the ZIP listing of the macro check, is
the sandbox's.

**The bytes decide; a name or mime naming an active kind also blocks, never unblocks** (stricter than the bytes alone,
and the only way to see the kinds without magic: a raw disk image, scripts without `#!`). Order: head magic, then the
container's macros, then the name's extension, then the mime type, then the fail-closed rule.

**Fail closed.** When the file needs the container check (`needs_container_check`: a ZIP or OLE2 head, or a name or mime of an
OOXML / ODF / ZIP family) and no checked answer exists (no facts, `TOO_LARGE`, `ERROR`), the kind is `UNCHECKED`: the
caller refuses with an honest "could not be checked" note, never sends the file unchecked.

Kinds (the RFC's table; the extension and mime lists go beyond the RFC's examples, which "also blocks" allows):

| Kind           | Bytes                                                         | Name / mime                                   |
|----------------|---------------------------------------------------------------|-----------------------------------------------|
| HTA            | `<hta:application` in a markup head                           | .hta; application/hta                          |
| HTML           | markup head with `<!doctype html`, `<html`, or (no `<svg`) `<head`/`<body`/`<script`/`<iframe` | .html .htm .xhtml .shtml .mht .mhtml; text/html, application/xhtml+xml, multipart/related |
| SVG            | `<svg` in a markup head                                       | .svg .svgz; image/svg+xml                      |
| OFFICE_MACROS  | container: `vbaProject.bin`, a macro-enabled content type, ODF `Basic/`/`Scripts/`, an OLE2 VBA storage | .docm .dotm .xlsm .xltm .xlam .pptm .potm .ppsm .ppam .sldm; a mime with `macroenabled` |
| SHORTCUT       | the shell-link header (`4C 00 00 00` + LNK CLSID)              | .lnk .url .scf; application/x-ms-shortcut      |
| DISK_IMAGE     | `CD001` / `BEA01` / `NSR02` / `NSR03` at 32 769 (also 34 817, 36 865 for CD001) | .iso .img .vhd .vhdx .dmg; application/x-iso9660-image |
| EXECUTABLE     | `document_kind.sniff_kind`'s `executable` (ELF, Mach-O, MZ), plus MZ and every Mach-O magic at offset 0 | .exe .dll .msi .msix .msp .appx .com .scr .pif .cpl .jar .apk; application/x-msdownload, … |
| SCRIPT         | a `#!` first line                                             | .js .jse .vbs .vbe .ps1 .psm1 .bat .cmd .sh .command .wsf .wsh .reg; application/javascript, … |

Public interface:
    ACTIVE_HEAD_BYTES
    HTA, HTML, SVG, OFFICE_MACROS, SHORTCUT, DISK_IMAGE, EXECUTABLE, SCRIPT, UNCHECKED     the kind ids
    ACTIVE_KIND_LABELS                       kind id -> the words a note uses ("an HTML page", …)
    needs_container_check(name, mime, head) -> bool
    active_kind(name, mime, head, container) -> Optional[str]      the kind id, or None (the file may be sent)
"""
from __future__ import annotations

import os
from typing import Dict, FrozenSet, Mapping, Optional, Tuple

from .container_facts import ContainerFacts
from .document_kind import sniff_kind

ACTIVE_HEAD_BYTES = 64 * 1024

HTA = "hta"
HTML = "html"
SVG = "svg"
OFFICE_MACROS = "office_macros"
SHORTCUT = "shortcut"
DISK_IMAGE = "disk_image"
EXECUTABLE = "executable"
SCRIPT = "script"
UNCHECKED = "unchecked_container"

ACTIVE_KIND_LABELS: Mapping[str, str] = {
    HTA: "an HTML application (HTA)",
    HTML: "an HTML page",
    SVG: "an SVG image",
    OFFICE_MACROS: "an Office document with macros",
    SHORTCUT: "a shortcut (LNK)",
    DISK_IMAGE: "a disk image",
    EXECUTABLE: "an executable program",
    SCRIPT: "a script",
    UNCHECKED: "an Office or ZIP file whose contents could not be checked for macros",
}

_EXT_KIND: Dict[str, str] = {
    ".hta": HTA,
    ".html": HTML, ".htm": HTML, ".xhtml": HTML, ".shtml": HTML, ".mht": HTML, ".mhtml": HTML,
    ".svg": SVG, ".svgz": SVG,
    ".docm": OFFICE_MACROS, ".dotm": OFFICE_MACROS, ".xlsm": OFFICE_MACROS, ".xltm": OFFICE_MACROS,
    ".xlam": OFFICE_MACROS, ".pptm": OFFICE_MACROS, ".potm": OFFICE_MACROS, ".ppsm": OFFICE_MACROS,
    ".ppam": OFFICE_MACROS, ".sldm": OFFICE_MACROS,
    ".lnk": SHORTCUT, ".url": SHORTCUT, ".scf": SHORTCUT,
    ".iso": DISK_IMAGE, ".img": DISK_IMAGE, ".vhd": DISK_IMAGE, ".vhdx": DISK_IMAGE, ".dmg": DISK_IMAGE,
    ".exe": EXECUTABLE, ".dll": EXECUTABLE, ".msi": EXECUTABLE, ".msix": EXECUTABLE, ".msp": EXECUTABLE,
    ".appx": EXECUTABLE, ".com": EXECUTABLE, ".scr": EXECUTABLE, ".pif": EXECUTABLE, ".cpl": EXECUTABLE,
    ".jar": EXECUTABLE, ".apk": EXECUTABLE,
    ".js": SCRIPT, ".jse": SCRIPT, ".vbs": SCRIPT, ".vbe": SCRIPT, ".ps1": SCRIPT, ".psm1": SCRIPT,
    ".bat": SCRIPT, ".cmd": SCRIPT, ".sh": SCRIPT, ".command": SCRIPT, ".wsf": SCRIPT, ".wsh": SCRIPT,
    ".reg": SCRIPT,
}
_MIME_KIND: Dict[str, str] = {
    "application/hta": HTA,
    "text/html": HTML, "application/xhtml+xml": HTML, "multipart/related": HTML,
    "image/svg+xml": SVG,
    "application/x-ms-shortcut": SHORTCUT, "application/x-mswinurl": SHORTCUT,
    "application/x-iso9660-image": DISK_IMAGE, "application/x-apple-diskimage": DISK_IMAGE,
    "application/x-msdownload": EXECUTABLE, "application/x-msdos-program": EXECUTABLE,
    "application/x-ms-installer": EXECUTABLE, "application/x-msi": EXECUTABLE,
    "application/x-executable": EXECUTABLE, "application/x-mach-binary": EXECUTABLE,
    "application/x-sharedlib": EXECUTABLE, "application/java-archive": EXECUTABLE,
    "application/vnd.android.package-archive": EXECUTABLE,
    "application/javascript": SCRIPT, "text/javascript": SCRIPT, "application/x-javascript": SCRIPT,
    "text/vbscript": SCRIPT, "application/x-sh": SCRIPT, "application/x-shellscript": SCRIPT,
    "application/x-bat": SCRIPT, "application/x-powershell": SCRIPT,
}
_MACRO_MIME_MARKER = "macroenabled"

# ZIP families whose macros live inside the container: OOXML and ODF documents, and any ZIP.
_CONTAINER_EXTS: FrozenSet[str] = frozenset({
    ".docx", ".docm", ".dotx", ".dotm", ".xlsx", ".xlsm", ".xltx", ".xltm", ".xlsb", ".xlam",
    ".pptx", ".pptm", ".potx", ".potm", ".ppsx", ".ppsm", ".ppam", ".sldx", ".sldm",
    ".odt", ".ott", ".ods", ".ots", ".odp", ".otp", ".odg", ".otg", ".odb", ".zip",
})
_CONTAINER_MIME_PREFIXES: Tuple[str, ...] = (
    "application/vnd.openxmlformats-officedocument.", "application/vnd.ms-word.", "application/vnd.ms-excel.",
    "application/vnd.ms-powerpoint.", "application/vnd.oasis.opendocument.", "application/zip",
    "application/x-zip",
)

_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"   # OLE2 compound file: .doc/.xls/.ppt, encrypted OOXML
_LNK_HEADER = b"\x4c\x00\x00\x00\x01\x14\x02\x00\x00\x00\x00\x00\xc0\x00\x00\x00\x00\x00\x00\x46"
_ISO_MARKS: Tuple[Tuple[int, bytes], ...] = (
    (32769, b"CD001"), (34817, b"CD001"), (36865, b"CD001"),
    (32769, b"BEA01"), (32769, b"NSR02"), (32769, b"NSR03"),
)
_EXEC_MAGICS = (b"MZ", b"\x7fELF", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe",
                b"\xca\xfe\xba\xbe")
_UTF8_BOM = b"\xef\xbb\xbf"
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")
_MARKUP_STRIP = b" \t\r\n\x0c"
_HTML_STRONG = (b"<!doctype html", b"<html")
_HTML_WEAK = (b"<head", b"<body", b"<script", b"<iframe")


def _ext(name: str) -> str:
    """The extension a desktop would act on: lower-cased, trailing dots and spaces dropped (Windows drops them too)."""
    return os.path.splitext(name.strip().rstrip(". ").lower())[1]


def _bare_mime(mime: str) -> str:
    return mime.split(";", 1)[0].strip().lower()


def _markup(head: bytes) -> bytes:
    """The head as lower-case markup text when it starts like markup (`<` after a BOM and white space), else b""."""
    if head.startswith(_UTF16_BOMS):
        try:
            head = head.decode("utf-16", errors="ignore").encode("utf-8", errors="ignore")
        except (UnicodeError, ValueError):
            return b""
    text = head[len(_UTF8_BOM):] if head.startswith(_UTF8_BOM) else head
    text = text.lstrip(_MARKUP_STRIP)
    return text.lower() if text.startswith(b"<") else b""


def _head_kind(head: bytes) -> Optional[str]:
    if head.startswith(_LNK_HEADER):
        return SHORTCUT
    if any(head[offset:offset + len(mark)] == mark for offset, mark in _ISO_MARKS):
        return DISK_IMAGE
    if head.startswith(_EXEC_MAGICS) or sniff_kind(head, "other") == "executable":
        return EXECUTABLE
    bare = head[len(_UTF8_BOM):] if head.startswith(_UTF8_BOM) else head
    if bare.startswith(b"#!"):
        return SCRIPT
    markup = _markup(head)
    if not markup:
        return None
    if b"<hta:application" in markup:
        return HTA
    if any(tag in markup for tag in _HTML_STRONG):
        return HTML
    if b"<svg" in markup:
        return SVG
    if any(tag in markup for tag in _HTML_WEAK):
        return HTML
    return None


def _mime_kind(mime: str) -> Optional[str]:
    bare = _bare_mime(mime)
    if _MACRO_MIME_MARKER in bare:
        return OFFICE_MACROS
    return _MIME_KIND.get(bare)


def needs_container_check(name: str, mime: str, head: bytes) -> bool:
    """True when the macro check must run: a ZIP head, an OLE2 head (legacy Office under any name; owner decision
    2026-11-05), or a name or mime of the OOXML / ODF / ZIP families."""
    return (head.startswith(_ZIP_MAGICS) or head.startswith(_OLE_MAGIC) or _ext(name) in _CONTAINER_EXTS
            or _bare_mime(mime).startswith(_CONTAINER_MIME_PREFIXES))


def active_kind(name: str, mime: str, head: bytes, container: Optional[ContainerFacts]) -> Optional[str]:
    """The D13 kind of an email attachment, or None when it may be sent. `head` is at most `ACTIVE_HEAD_BYTES` of the
    file (more is ignored); `container` is the sandbox's answer for this file, None when no check ran."""
    head = head[:ACTIVE_HEAD_BYTES]
    by_head = _head_kind(head)
    if by_head:
        return by_head
    if container is not None and container.carries_macros:
        return OFFICE_MACROS
    by_name = _EXT_KIND.get(_ext(name))
    if by_name:
        return by_name
    by_mime = _mime_kind(mime)
    if by_mime:
        return by_mime
    if needs_container_check(name, mime, head) and (container is None or not container.checked):
        return UNCHECKED
    return None

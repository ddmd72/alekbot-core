"""
Content policy (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §5). Default-deny, decided from metadata first;
a download happens only for an admitted kind. Safety never depends on classifying correctly (§5.5):
these rules buy quality and cost; the bounds live elsewhere.
"""
from __future__ import annotations

import fnmatch
import os
from enum import Enum
from typing import Dict, FrozenSet, Optional, Sequence

from .drive_index import ContentClass, DriveChange, FolderProbe, Reason
from .drive_index_params import DENSE_FOLDER_CHILDREN, MAX_DOWNLOAD_BYTES, MAX_SHEET_DOWNLOAD_BYTES

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
_DOCUMENT_KINDS: FrozenSet[str] = frozenset({
    "pdf", "docx", "doc", "pptx", "ppt", "xlsx", "xls", "odt", "ods", "odp", "epub", "html", "mhtml",
    "markdown", "text", "csv", "json", "rtf", "msg", "eml", "subtitles", "iwork",
})
_MEDIA_KINDS: FrozenSet[str] = frozenset({"image", "audio", "video"})

# The Slice 2+4 admission (§5.1, §24.1), taken with the POLICY_VERSION 2 bump. Each content kind has a named
# extractor; doc, ppt, mhtml and iwork have none and stay out.
SLICE2_CONTENT_KINDS: FrozenSet[str] = frozenset({
    "pdf", "docx", "pptx", "xlsx", "xls", "odt", "ods", "odp", "epub", "html", "markdown", "text", "csv", "json",
    "rtf", "msg", "eml", "subtitles",
})
SLICE2_TRIAGED_KINDS: FrozenSet[str] = frozenset({"image"})

ADMITTED_KINDS: FrozenSet[str] = SLICE2_CONTENT_KINDS   # content read (§5.1)
TRIAGED_KINDS: FrozenSet[str] = SLICE2_TRIAGED_KINDS    # every image enters triage first (§5.3, §24.5)
TRANSCRIBED_KINDS: FrozenSet[str] = frozenset({"audio", "video"})   # transcribed, not parsed (§27.3, policy 3)
_SHEET_KINDS: FrozenSet[str] = frozenset({"xlsx", "xls"})


class Admission(str, Enum):
    """What the policy lets the indexer do with a kind's content (§5.1, §5.3)."""
    NONE = "none"          # metadata only (kind_not_admitted)
    CONTENT = "content"    # downloaded and extracted
    TRIAGE = "triage"      # a thumbnail is triaged first; only a `text` verdict admits the content
    TRANSCRIBE = "transcribe"   # audio and video: transcribed by the transcript stage, never parsed (§27.3)

_SECRET_PATTERNS = ("id_rsa*", "id_ed25519*", "*.pem", "*.p12", "*.pfx", "*.kdbx", "*.keychain*")
_OS_PATTERNS = ("~$*", ".~lock.*#", "thumbs.db", "desktop.ini", ".ds_store", "*.tmp")   # .~lock: LibreOffice
# Kept as records, never searchable: no card and no name keys (§3.4 lets a reason withhold the card).
_UNSEARCHABLE_REASONS: FrozenSet[Reason] = frozenset({Reason.OS_ARTIFACT})
_MARKERS = frozenset({".git", "pyvenv.cfg"})
# Folders only tools create, wherever they sit (RFC Δ16): an extracted repository without `.git` is still opaque.
_GENERATED_DIRS = frozenset({"node_modules", "__pycache__", "site-packages"})


def kind_of(name: str, mime: str, is_folder: bool) -> str:
    if is_folder:
        return "folder"
    stem, ext = os.path.splitext(name.lower())
    if ext == _PACKAGE_EXT and os.path.splitext(stem)[1] in _PACKAGED_EXTS:
        ext = os.path.splitext(stem)[1]
    kind = _EXT_KIND.get(ext)
    if kind == "iwork" and ext == ".key" and mime not in ("", "application/vnd.apple.keynote"):
        return "other"   # a .key that is not Keynote is a secret file, caught by item_rule first
    if kind:
        return kind
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    return "other"


def admission(kind: str) -> Admission:
    """Read at call time, so a test (or the flip) that rebinds ADMITTED_KINDS / TRIAGED_KINDS / TRANSCRIBED_KINDS is seen."""
    if kind in ADMITTED_KINDS:
        return Admission.CONTENT
    if kind in TRIAGED_KINDS:
        return Admission.TRIAGE
    if kind in TRANSCRIBED_KINDS:
        return Admission.TRANSCRIBE
    return Admission.NONE


def size_cap(kind: str) -> int:
    """Download cap by kind, checked on metadata before any download (§5.4, §15): sheets amplify most (Phase 0)."""
    return MAX_SHEET_DOWNLOAD_BYTES if kind in _SHEET_KINDS else MAX_DOWNLOAD_BYTES


def content_class_of(kind: str) -> ContentClass:
    if kind == "folder":
        return ContentClass.FOLDER
    if kind in _DOCUMENT_KINDS:
        return ContentClass.DOCUMENT
    if kind in _MEDIA_KINDS:
        return ContentClass.MEDIA
    return ContentClass.OTHER


def card_rank_of(content_class: ContentClass) -> int:
    return {ContentClass.DOCUMENT: 0, ContentClass.MEDIA: 1, ContentClass.OTHER: 2, ContentClass.FOLDER: 3}[content_class]


def is_dot_name(name: str) -> bool:
    return name.startswith(".")


def is_readme_name(name: str) -> bool:
    """`README`, `README.md`, `readme.txt`, ...: the file whose lead text a repository card carries (§5.2)."""
    return os.path.splitext(name)[0].casefold() == "readme"


def readme_readable() -> bool:
    """A README is read like any document (§5.2), so only once text content is admitted (Slice 2+4)."""
    return admission("markdown") is Admission.CONTENT or admission("text") is Admission.CONTENT


def is_marker_child(name: str) -> bool:
    return name in _MARKERS


def is_generated_dir(name: str) -> bool:
    return name.casefold() in _GENERATED_DIRS


def _matches(name: str, patterns: Sequence[str]) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatchcase(lowered, p) for p in patterns)


def item_rule(change: DriveChange) -> Optional[Reason]:
    """Never-download rules (§5.1), in order. None: the kind decides. OS artefacts come first: `.DS_Store` is one
    before it is a dot name."""
    if _matches(change.name, _OS_PATTERNS):
        return Reason.OS_ARTIFACT
    if is_dot_name(change.name):
        return Reason.DOT_NAME
    if _matches(change.name, _SECRET_PATTERNS) or (change.name.lower().endswith(".key")
                                                   and change.mime_type != "application/vnd.apple.keynote"):
        return Reason.SECRET_NAME
    if "remoteItem" in change.facets:
        return Reason.REMOTE_ITEM
    if "package" in change.facets:
        return Reason.PACKAGE
    if "malware" in change.facets:
        return Reason.MALWARE
    return None


def searchable(reason: Optional[Reason]) -> bool:
    """False for records kept only so the mirror is complete (OS and lock artefacts)."""
    return reason not in _UNSEARCHABLE_REASONS


def probe_from_children(children: Sequence[DriveChange]) -> FolderProbe:
    counts: Dict[ContentClass, int] = {c: 0 for c in ContentClass}
    marker = False
    for child in children:
        marker = marker or is_marker_child(child.name)
        counts[content_class_of(kind_of(child.name, child.mime_type, child.is_folder))] += 1
    return FolderProbe(marker_found=marker, class_counts=counts, sampled=len(children))


def is_dense(child_count: int, probe: FolderProbe) -> bool:
    """Class 1 density (§5.5): many children, mostly neither document nor media."""
    if child_count <= DENSE_FOLDER_CHILDREN or probe.sampled == 0:
        return False
    files = probe.sampled - probe.class_counts.get(ContentClass.FOLDER, 0)
    if files <= 0:
        return False
    return probe.class_counts.get(ContentClass.OTHER, 0) * 2 > files


def density_still_valid(previous_count: int, child_count: int) -> bool:
    """Hysteresis (§5.2): a density decision stands until childCount doubles or halves."""
    if previous_count <= 0:
        return False
    return previous_count / 2 < child_count < previous_count * 2

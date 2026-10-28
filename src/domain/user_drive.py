"""
User drive — the user's long-term file area (docs/10_rfcs/USER_DRIVE_RFC.md).

A domain concept, not a provider: nothing here (refs, labels, names, errors) names the
storage provider. The adapter behind UserDrivePort owns the provider's ids and paths;
the model and session history only ever see `drive:<opaque id>` refs (§4.3).
"""
from __future__ import annotations

import mimetypes
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence

DRIVE_REF_PREFIX = "drive:"
DEFAULT_INBOX_FOLDER = "Inbox"

# Size caps, all checked on metadata before any download (§4.6, §4.9).
MAX_DRIVE_DOWNLOAD_BYTES = 45 * 1024 * 1024       # = FileManagementAgent's video re-send cap
MAX_DRIVE_VISION_IMAGE_BYTES = 5 * 1024 * 1024    # below the strictest provider image limit
MAX_DRIVE_VISION_PDF_BYTES = 20 * 1024 * 1024     # below the strictest provider PDF/inline limit
MAX_DRIVE_APPEND_FILE_BYTES = 5 * 1024 * 1024

MAX_DRIVE_TEXT_FILE_BYTES = 5 * 1024 * 1024           # encoded size of text the bot writes (§4.7.1); = the append-target cap

TEXT_FILE_EXTENSIONS = (".md", ".txt", ".csv", ".json", ".yaml", ".yml")
# Extensions a new text file may be created with (§4.7.1) and the content type each implies.
CREATABLE_TEXT_MIME_TYPES: Dict[str, str] = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".html": "text/html",
    ".svg": "image/svg+xml",
    ".xml": "application/xml",
}
# Image formats every LLM provider accepts as vision input (§4.6). HEIC (iPhone default) is not one.
VISION_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})

# A ref copied out of a label may carry the label's punctuation: `ref=drive:x]`, `"drive:x"`.
_REF_RE = re.compile(r"drive:([^\s\"'\]\[)(,]+)")
# Delivered documents are keyed {prefix}/{user_id}/{uuid4}-{filename} (DocumentDeliveryService).
_UUID_PREFIX_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-", re.I)
# Characters OneDrive and most drives forbid in a name, plus control characters.
_FORBIDDEN_RE = re.compile(r'["*:<>?/\\|\x00-\x1f]')
# Extensions Python's built-in `mimetypes` table does not know (it only learns them from
# /etc/mime.types, absent on a slim image), so a requested "Отчёт.docx" is not doubled to
# "Отчёт.docx.docx" in production.
_EXTRA_KNOWN_EXTENSIONS = frozenset({
    ".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt", ".odt", ".ods", ".odp",
    ".yaml", ".yml", ".toml", ".m4a", ".mkv", ".flac", ".7z", ".rar", ".epub", ".ics",
})


class DriveNotConnectedError(Exception):
    """No usable access: not connected, or the access expired / was revoked."""


class DriveItemNotFoundError(FileNotFoundError):
    """The item does not exist (deleted, or outside the app area)."""


class DriveRootProtectedError(Exception):
    """An operation would delete or move the area root."""


class DriveMirrorWriteRefused(Exception):
    """A chat write whose target, source or destination is the bot's transcript folder, lies under it, or contains it
    (USER_DRIVE_INDEXING_RFC §27.9.8, the Step 1 guard). The agent answers with one localized line."""


class DrivePathError(ValueError):
    """A request the drive rules refuse (bad path, wrong item kind, wrong mode)."""


class DriveNameConflictError(Exception):
    """The provider refused a create because the name is taken."""


class DriveStorageFull(Exception):
    """The provider refused a write: the drive is full."""


class DrivePathTooLong(Exception):
    """The provider refused a write because the resulting path is too long."""


@dataclass(frozen=True)
class DriveItem:
    """One file or folder in the area. `path` is relative to the area root ('' for the root)."""
    item_id: str
    name: str
    path: str
    is_folder: bool
    size_bytes: int = 0
    mime_type: str = ""
    modified_at: Optional[datetime] = None
    web_url: str = ""
    child_count: int = 0
    ctag: str = ""

    @property
    def ref(self) -> str:
        return make_drive_ref(self.item_id)


class DriveFileTooLargeError(Exception):
    """A download was refused before fetching because the file exceeds a cap."""

    def __init__(self, item: DriveItem, limit_bytes: int) -> None:
        super().__init__(f"{item.path} is {format_size(item.size_bytes)} (limit {format_size(limit_bytes)})")
        self.item = item
        self.limit_bytes = limit_bytes


class DriveFileTooLarge(Exception):
    """A streamed download passed its byte cap (USER_DRIVE_INDEXING_RFC §5.4, §8.1 step 3): metadata can understate a
    size, so the stream is cut and the partial file is the caller's to delete."""

    def __init__(self, limit_bytes: int) -> None:
        super().__init__(f"download exceeded {format_size(limit_bytes)}")
        self.limit_bytes = limit_bytes


@dataclass
class FolderResolution:
    folder: DriveItem
    created: List[str] = field(default_factory=list)


@dataclass
class SaveOutcome:
    item: DriveItem
    created: List[str]
    already_existed: bool = False
    renamed: bool = False


@dataclass
class ListOutcome:
    folder: DriveItem
    items: List[DriveItem]
    truncated: bool


@dataclass
class MoveOutcome:
    before: DriveItem
    after: DriveItem
    created: List[str]


@dataclass
class DeleteOutcome:
    item: DriveItem
    file_count: int
    count_capped: bool = False


@dataclass
class UpdateOutcome:
    item: DriveItem
    before_size: int
    mode: str  # "append" | "replace"


def make_drive_ref(item_id: str) -> str:
    return f"{DRIVE_REF_PREFIX}{item_id}"


def parse_drive_ref(ref: str) -> Optional[str]:
    """Item id from a `drive:` ref, tolerating label punctuation; None for any other ref."""
    if not ref or DRIVE_REF_PREFIX not in ref:
        return None
    match = _REF_RE.search(ref)
    return match.group(1) if match else None


def is_drive_ref(ref: str) -> bool:
    return parse_drive_ref(ref) is not None


def split_folder_path(path: str) -> List[str]:
    """Normalise a user-typed folder path into segments (§4.8)."""
    if "\\" in path:
        raise DrivePathError(f"Use '/' between folders: {path!r}")
    segments = [s.strip() for s in path.split("/") if s.strip()]
    for segment in segments:
        if segment in (".", ".."):
            raise DrivePathError(f"'{segment}' is not a folder name")
    return segments


def name_key(name: str) -> str:
    """Comparison key for names: Unicode NFC + case folding. Files from macOS/iOS arrive in NFD (§4.7)."""
    return unicodedata.normalize("NFC", name).casefold()


def match_child_folder(children: Sequence[DriveItem], name: str) -> Optional[DriveItem]:
    """The child folder named `name`: exact match first, else by `name_key` (§4.8)."""
    folders = [c for c in children if c.is_folder]
    for child in folders:
        if child.name == name:
            return child
    wanted = name_key(name)
    for child in folders:
        if name_key(child.name) == wanted:
            return child
    return None


def join_drive_path(parent: str, name: str) -> str:
    return f"{parent}/{name}" if parent else name


def format_size(size_bytes: int) -> str:
    if size_bytes >= 1_048_576:
        return f"{size_bytes / 1_048_576:.1f}MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.0f}KB"
    return f"{size_bytes}B"


def drive_label(item: DriveItem) -> str:
    """The label the model sees (§4.3). Never names a provider."""
    if item.is_folder:
        shown = f"{item.path}/" if item.path else "/"
        return f"[Drive folder: {shown} ref={item.ref}]"
    return f"[Drive: {item.path} ({format_size(item.size_bytes)}) ref={item.ref}]"


# The one sentence a move with neither a folder nor a new name is refused with: UserDriveService raises it, and the
# drive specialist's move tool says it before any call (DRIVE_SPECIALIST_RFC §8.1).
MOVE_NEEDS_TARGET = "Give a destination folder, a new name, or both"


def drive_context_entry(item: DriveItem) -> Dict[str, str]:
    """One entry of the `drive_context` history block (§4.3)."""
    return {"path": item.path or "/", "ref": item.ref}

# The chat store's Finder-style dedup suffix (GcsFileStorageAdapter._deduplicate): "<stem> (N)<ext>".
_CHAT_DEDUP_SUFFIX_RE = re.compile(r"^(?P<stem>.+) \(\d+\)$")


def strip_chat_dedup_suffix(name: str) -> str:
    """The user's file name behind a chat-store dedup key: "a (1).md" -> "a.md".

    The chat store renames a re-sent file to "<stem> (N)<ext>" and keeps no record of the original
    name, so the suffix is stripped by shape. Edge case: a user file literally named "x (1).md" is
    saved as "x.md" (the name is only a default; an explicit `name` always wins)."""
    stem, ext = os.path.splitext(name)
    match = _CHAT_DEDUP_SUFFIX_RE.match(stem)
    return f"{match.group('stem')}{ext}" if match else name


def sanitize_drive_filename(name: str) -> str:
    cleaned = _FORBIDDEN_RE.sub("_", name).strip().rstrip(". ")
    return cleaned or "file"


def drive_filename(requested: Optional[str], source_ref: str) -> str:
    """The name a saved file gets (§4.7): requested if given, else the source name without
    a delivered-document uuid prefix and the chat store's " (N)" dedup suffix (so a re-sent file
    keeps the name the user uploaded and the service's duplicate check can see it). The source extension is appended unless the requested
    name already has a known file extension — "Договор v2.1" must not lose ".pdf" to a dotted
    version, "notes.md" keeps its own."""
    source_name = _UUID_PREFIX_RE.sub("", source_ref.rsplit("/", 1)[-1])
    if not requested or not requested.strip():
        return sanitize_drive_filename(strip_chat_dedup_suffix(source_name))
    name = sanitize_drive_filename(requested)
    source_ext = os.path.splitext(source_name)[1]
    if source_ext and not _has_known_extension(name, source_ext):
        name = f"{name}{source_ext}"
    return name


def _has_known_extension(name: str, source_ext: str) -> bool:
    """True when `name` already ends in a file extension: the source's own, one the
    `mimetypes` table knows, or one from `_EXTRA_KNOWN_EXTENSIONS`."""
    ext = os.path.splitext(name)[1].lower()
    if not ext:
        return False
    return (
        ext == source_ext.lower()
        or ext in _EXTRA_KNOWN_EXTENSIONS
        or mimetypes.guess_type(name)[0] is not None
    )


def prepare_drive_text_file(name: Optional[str], text: str) -> "tuple[str, bytes, str]":
    """Validate and encode text the bot wrote (§4.7.1): (filename, bytes, content type).

    The extension of `name` decides the content type and must be a creatable text one; the name is
    sanitized like any drive name. UTF-8 without BOM, line endings normalised to LF, otherwise as
    given. Raises DrivePathError (no I/O happened)."""
    if not name or not name.strip():
        raise DrivePathError('"name" is required with "text", with an extension such as notes.md.')
    filename = sanitize_drive_filename(name)
    ext = os.path.splitext(filename)[1].lower()
    if not ext and filename.lower() in CREATABLE_TEXT_MIME_TYPES:   # ".md": an extension with no stem
        raise DrivePathError(f'"name" needs a file name before the extension, e.g. notes{filename}.')
    mime = CREATABLE_TEXT_MIME_TYPES.get(ext)
    if mime is None:
        allowed = " ".join(CREATABLE_TEXT_MIME_TYPES)
        raise DrivePathError(
            f"Text files can be created only as {allowed}; '{name}' is not one of them. Images, PDF and "
            f"DOCX come from generate_image, create_pdf or create_document: produce the file, then save "
            f"it with file_ref.")
    # "UTF-8 without BOM": a leading U+FEFF in the model's text would encode as one, so it is dropped.
    if text.startswith("\ufeff"):
        text = text[1:]
    try:
        data = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    except UnicodeEncodeError as e:   # a lone surrogate from the model's JSON: not valid text
        raise DrivePathError(f"The text has an invalid character at position {e.start} and cannot be "
                             f"encoded as UTF-8; rewrite it with plain characters.") from e
    if len(data) > MAX_DRIVE_TEXT_FILE_BYTES:
        raise DrivePathError(f"The text is {format_size(len(data))}; text files are limited to "
                             f"{format_size(MAX_DRIVE_TEXT_FILE_BYTES)}.")
    return filename, data, mime


def is_text_file(name: str, mime_type: str) -> bool:
    return mime_type.startswith("text/") or name.lower().endswith(TEXT_FILE_EXTENSIONS)


class DriveResyncRequired(Exception):
    """A full resync is required (cursor expired, HTTP 410): restart from `restart_cursor` as a full
    enumeration (§7.5)."""

    def __init__(self, restart_cursor: str) -> None:
        super().__init__("Drive change cursor expired; full enumeration required")
        self.restart_cursor = restart_cursor


class DriveReauthRequired(DriveNotConnectedError):
    """The token refresh itself was rejected (invalid_grant): only a reconnect helps (§4, §7.7)."""


class DriveAccessDenied(Exception):
    """A 401 that survived a successful token refresh: an operation error, never a lost grant (§4). A 403 stays a per-item ValueError."""


@dataclass(frozen=True)
class DriveConnection:
    """What the gate and search read about the user's connection (§7.1 step 0, §10.1 step 0)."""
    connected: bool
    needs_reconnect: bool = False
    drive_id: str = ""


class DriveThrottled(Exception):
    """The provider kept answering 429/503 after the adapter's own retries (§7.7): end the tick."""

    def __init__(self, retry_after_s: float) -> None:
        super().__init__(f"Drive provider throttled; retry after {retry_after_s:.0f}s")
        self.retry_after_s = retry_after_s

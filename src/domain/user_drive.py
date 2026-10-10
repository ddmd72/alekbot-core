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

TEXT_FILE_EXTENSIONS = (".md", ".txt", ".csv", ".json", ".yaml", ".yml")
# Image formats every LLM provider accepts as vision input (§4.6). HEIC (iPhone default) is not one.
VISION_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})

# A ref copied out of a label may carry the label's punctuation: `ref=drive:x]`, `"drive:x"`.
_REF_RE = re.compile(r"drive:([^\s\"'\]\[)(,]+)")
# Delivered documents are keyed {prefix}/{user_id}/{uuid4}-{filename} (DocumentDeliveryService).
_UUID_PREFIX_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-", re.I)
# Characters OneDrive and most drives forbid in a name, plus control characters.
_FORBIDDEN_RE = re.compile(r'["*:<>?/\\|\x00-\x1f]')


class DriveNotConnectedError(Exception):
    """No usable access: not connected, or the access expired / was revoked."""


class DriveItemNotFoundError(FileNotFoundError):
    """The item does not exist (deleted, or outside the app area)."""


class DriveRootProtectedError(Exception):
    """An operation would delete or move the area root."""


class DrivePathError(ValueError):
    """A request the drive rules refuse (bad path, wrong item kind, wrong mode)."""


class DriveNameConflictError(Exception):
    """The provider refused a create because the name is taken."""


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

    @property
    def ref(self) -> str:
        return make_drive_ref(self.item_id)


class DriveFileTooLargeError(Exception):
    """A download was refused before fetching because the file exceeds a cap."""

    def __init__(self, item: DriveItem, limit_bytes: int) -> None:
        super().__init__(f"{item.path} is {format_size(item.size_bytes)} (limit {format_size(limit_bytes)})")
        self.item = item
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


def drive_context_entry(item: DriveItem) -> Dict[str, str]:
    """One entry of the `drive_context` history block (§4.3)."""
    return {"path": item.path or "/", "ref": item.ref}


def sanitize_drive_filename(name: str) -> str:
    cleaned = _FORBIDDEN_RE.sub("_", name).strip().rstrip(". ")
    return cleaned or "file"


def drive_filename(requested: Optional[str], source_ref: str) -> str:
    """The name a saved file gets (§4.7): requested if given, else the source name without
    a delivered-document uuid prefix. The source extension is appended unless the requested
    name already has a known file extension — "Договор v2.1" must not lose ".pdf" to a dotted
    version, "notes.md" keeps its own."""
    source_name = _UUID_PREFIX_RE.sub("", source_ref.rsplit("/", 1)[-1])
    if not requested or not requested.strip():
        return sanitize_drive_filename(source_name)
    name = sanitize_drive_filename(requested)
    source_ext = os.path.splitext(source_name)[1]
    if source_ext and mimetypes.guess_type(name)[0] is None:
        name = f"{name}{source_ext}"
    return name


def is_text_file(name: str, mime_type: str) -> bool:
    return mime_type.startswith("text/") or name.lower().endswith(TEXT_FILE_EXTENSIONS)

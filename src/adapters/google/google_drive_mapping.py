"""Google Drive v3 → the neutral drive shapes (GOOGLE_DRIVE_RFC §3.4, §3.5, §3.7). Pure functions, no I/O.

- Items: `to_change` / `to_item` read one Drive `files` resource (the field mask `FILE_FIELDS`) and build the
  `DriveChange` / `DriveItem` fields of RFC §3.4: identities `{id_space}:{fileId}` with no account part (GE-2), the
  drive identity `{id_space}:account:{permissionId}` (GE-1), the export mime of a native (GE-6), `content_version`
  (GE-3, GE-4), `ContentIdentity("md5", …)` for binaries only (GE-5), `ItemAccess` from `capabilities`, typed media.
  The change feed defers a native's head revision (GE-36): `to_change(defer_head=True)` emits it pending with the
  `m:` key, `resolved_change` gives it its final key once the revision list was read (or refused).
- Errors: `classify` maps an HTTP status and the error body's `reason` to one outcome class (§3.7). A 403 is read by its
  reason, never by its status alone: Google answers 403 both for a rate limit and for a refused permission (GE-18).
- Queries: `escape_query` escapes `'` and `\\` with `\\` [DOC search].
- Thumbnails: `sized_thumbnail` sets a `thumbnailLink`'s `=s<n>` suffix [PROBE P-f].
"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Tuple

from ...domain.drive_content_identity import ContentIdentity
from ...domain.drive_index import DriveChange
from ...domain.drive_item_access import ItemAccess
from ...domain.drive_media_facts import DriveMediaFacts
from ...domain.user_drive import DriveItem, join_drive_path

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
_NATIVE_PREFIX = "application/vnd.google-apps."
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# GE-6 (§3.5): the export of each native the parser reads; any other native (Form, Site, Map, …) is metadata only.
EXPORT_MIMES: Mapping[str, str] = {
    "application/vnd.google-apps.document": "text/markdown",
    "application/vnd.google-apps.spreadsheet": XLSX_MIME,
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.drawing": "application/pdf",
}
CONTENT_FAMILY = "md5"
# The field mask F of §3.1 (every file read), plus nothing else.
FILE_FIELDS = (
    "id,name,mimeType,parents,size,md5Checksum,headRevisionId,modifiedTime,createdTime,trashed,webViewLink,"
    "shortcutDetails(targetId,targetMimeType),capabilities(canDownload,canModifyContent,canRename,"
    "canMoveItemWithinDrive,canTrash,canAddChildren,canReadRevisions),imageMediaMetadata(time,width,height,"
    "cameraMake,cameraModel,location),videoMediaMetadata(durationMillis,width,height),thumbnailLink,driveId"
)

# Outcome classes of `classify` (§3.7).
RETRY = "retry"                      # rate limit, 429, 5xx: backoff, then DriveThrottled
DAILY_LIMIT = "daily_limit"          # DriveThrottled(TICK_DEADLINE_S), no retry
ACCESS_DENIED = "access_denied"      # DriveAccessDenied
STORAGE_FULL = "storage_full"        # DriveStorageFull
PATH_TOO_LONG = "path_too_long"      # DrivePathTooLong
EXPORT_TOO_LARGE = "export_too_large"  # DriveFileTooLarge(Error)
NOT_FOUND = "not_found"              # DriveItemNotFoundError, never a deletion
UNAUTHORIZED = "unauthorized"        # one forced refresh, then DriveAccessDenied
BAD_REQUEST = "bad_request"          # a 400: the caller decides (a refused page token), else ValueError
OTHER = "other"                      # ValueError with the status and reason only

_RATE_REASONS = frozenset({"userRateLimitExceeded", "rateLimitExceeded"})
_DENIED_REASONS = frozenset({"insufficientFilePermissions", "appNotAuthorizedToFile", "domainPolicy"})
_REASON_RE = re.compile(r"[A-Za-z]{1,64}")
_THUMB_SIZE_RE = re.compile(r"=s\d+$")


def _section(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """A nested object of the resource; {} when absent or not an object."""
    value = raw.get(key)
    return value if isinstance(value, Mapping) else {}


# -- identities ------------------------------------------------------------------------------------------------------


def item_identity(id_space: str, file_id: str) -> str:
    """`{id_space}:{fileId}` (GE-2): a file id is the same for every account that sees it. "" for an unknown id."""
    return f"{id_space}:{file_id}" if file_id else ""


def drive_identity(id_space: str, permission_id: str) -> str:
    """`{id_space}:account:{permissionId}` (GE-1): the `account:` part keeps it from ever equalling an item identity."""
    return f"{id_space}:account:{permission_id}" if permission_id else ""


# -- kinds of resource -----------------------------------------------------------------------------------------------


def is_folder(raw: Mapping[str, Any]) -> bool:
    return raw.get("mimeType") == FOLDER_MIME


def is_shortcut(raw: Mapping[str, Any]) -> bool:
    return raw.get("mimeType") == SHORTCUT_MIME


def is_native(raw: Mapping[str, Any]) -> bool:
    """A Google-native document (Docs, Sheets, …, Forms): no bytes of its own; folders and shortcuts are not natives."""
    mime = str(raw.get("mimeType") or "")
    return mime.startswith(_NATIVE_PREFIX) and mime not in (FOLDER_MIME, SHORTCUT_MIME)


def export_mime(raw: Mapping[str, Any]) -> Optional[str]:
    """The export mime of an exportable native (GE-6), None for anything else."""
    return EXPORT_MIMES.get(str(raw.get("mimeType") or ""))


def can_read_revisions(raw: Mapping[str, Any]) -> bool:
    caps = _section(raw, "capabilities")
    return caps.get("canReadRevisions") is True


def head_revision_due(raw: Mapping[str, Any]) -> bool:
    """An exportable native, not trashed, whose `canReadRevisions` is true: its change key is `r:{head revision id}`,
    which costs a `revisions.list` read (G6, GE-4). Anything else has its key from the resource alone."""
    return is_native(raw) and export_mime(raw) is not None and can_read_revisions(raw) and raw.get("trashed") is not True


def parent_of(raw: Mapping[str, Any]) -> str:
    """The item's one parent (Drive keeps a single parent [DOC folder]); "" when it has no visible parent."""
    parents = raw.get("parents")
    return str(parents[0]) if isinstance(parents, list) and parents else ""


# -- field values ----------------------------------------------------------------------------------------------------


def parse_time(value: Any) -> Optional[datetime]:
    """An RFC 3339 time (`2026-01-15T09:00:00.000Z`) as an aware datetime, None when absent or unreadable."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def parse_exif_time(value: Any) -> Optional[datetime]:
    """`imageMediaMetadata.time` (`YYYY:MM:DD HH:MM:SS`, no zone) with `tzinfo=timezone.utc` attached; None when
    unparseable. The camera's zone is unknown, so UTC is a labelled convention (design Δ15): `DriveMediaFacts` refuses a
    naive `taken_at`."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _int(value: Any) -> int:
    """A non-negative int from Drive's JSON (int64 fields arrive as strings); 0 for anything else."""
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number >= 0 else 0


def _float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def media_facts(raw: Mapping[str, Any], id_space: str) -> DriveMediaFacts:
    """Typed facts (§3.4): photo time, camera and location and image size from `imageMediaMetadata`, a video's length
    and frame from `videoMediaMetadata`; a shortcut is an alias of `{id_space}:{targetId}`, never followed. Drive has no
    audio, package or malware facet: those stay at their defaults."""
    image = _section(raw, "imageMediaMetadata")
    video = _section(raw, "videoMediaMetadata")
    location = _section(image, "location")
    lat, lon = _float(location.get("latitude")), _float(location.get("longitude"))
    alias_of: Optional[str] = None
    if is_shortcut(raw):
        details = _section(raw, "shortcutDetails")
        alias_of = item_identity(id_space, str(details.get("targetId") or ""))
    return DriveMediaFacts(
        taken_at=parse_exif_time(image.get("time")),
        camera_make=str(image.get("cameraMake") or ""),
        camera_model=str(image.get("cameraModel") or ""),
        latitude=lat if lat is not None and lon is not None else None,
        longitude=lon if lat is not None and lon is not None else None,
        image_width=_int(image.get("width")),
        image_height=_int(image.get("height")),
        video_duration_ms=_int(video.get("durationMillis")),
        video_width=_int(video.get("width")),
        video_height=_int(video.get("height")),
        alias_of=alias_of,
    )


def content_identity(raw: Mapping[str, Any]) -> Optional[ContentIdentity]:
    """`ContentIdentity("md5", md5Checksum, size)` of a binary (GE-5); None for a native, a folder, a shortcut or a
    binary Drive gave no checksum for."""
    digest = raw.get("md5Checksum")
    if not digest or is_folder(raw) or is_shortcut(raw) or is_native(raw):
        return None
    return ContentIdentity(CONTENT_FAMILY, str(digest), _int(raw.get("size")))


def binary_content_version(raw: Mapping[str, Any]) -> str:
    """GE-3: `headRevisionId` (a rename leaves it), else `md5:{md5Checksum}`, else ""."""
    head = raw.get("headRevisionId")
    if head:
        return str(head)
    digest = raw.get("md5Checksum")
    return f"md5:{digest}" if digest else ""


def native_content_version(head_revision_id: Optional[str], modified_time: Any) -> str:
    """GE-4: `r:{head revision id}` when the revision list was read, else `m:{modifiedTime}`."""
    if head_revision_id:
        return f"r:{head_revision_id}"
    return f"m:{modified_time}" if modified_time else ""


def content_version(raw: Mapping[str, Any], head_revision_id: Optional[str] = None) -> str:
    """The change key of §3.4: "" for a folder, a shortcut or an un-exportable native; a native's by GE-4 (the caller
    read its revisions, or not); a binary's by GE-3."""
    if is_folder(raw) or is_shortcut(raw):
        return ""
    if is_native(raw):
        return native_content_version(head_revision_id, raw.get("modifiedTime")) if export_mime(raw) else ""
    return binary_content_version(raw)


def mime_of(raw: Mapping[str, Any]) -> str:
    """The neutral mime (§3.4, GE-20): a binary's own, a native's export mime, "" for a folder, a shortcut or an
    un-exportable native. No Google-own mime leaves this module."""
    if is_folder(raw) or is_shortcut(raw):
        return ""
    if is_native(raw):
        return export_mime(raw) or ""
    return str(raw.get("mimeType") or "")


def size_of(raw: Mapping[str, Any]) -> int:
    """`size` for a binary and a native (storage bytes, GE-21); 0 for a folder or a shortcut."""
    if is_folder(raw) or is_shortcut(raw):
        return 0
    return _int(raw.get("size"))


def access_of(raw: Mapping[str, Any]) -> ItemAccess:
    """`ItemAccess` from `capabilities` (§3.4): a native never takes content writes (D10), a shortcut or an
    un-exportable native has nothing to download. `ownedByMe` is never read."""
    caps = _section(raw, "capabilities")
    native = is_native(raw)
    exportable = not native or export_mime(raw) is not None
    downloadable = caps.get("canDownload") is True and not is_shortcut(raw) and exportable
    return ItemAccess(
        can_download=downloadable,
        can_write_content=caps.get("canModifyContent") is True and not native,
        can_rename_move=caps.get("canRename") is True and caps.get("canMoveItemWithinDrive") is True,
        can_delete=caps.get("canTrash") is True,
        can_add_children=caps.get("canAddChildren") is True,
    )


# -- neutral shapes --------------------------------------------------------------------------------------------------


def to_change(raw: Mapping[str, Any], id_space: str, head_revision_id: Optional[str] = None, *,
              defer_head: bool = False) -> DriveChange:
    """A `files` resource as an observation. A trashed item is a deletion (§3.3). `head_revision_id` is a native's
    head revision when the caller read it (GE-4). `defer_head` (the change feed): an item whose key needs the revision
    read (`head_revision_due`) comes back pending with its `m:` fallback key, resolved later by `resolved_change`."""
    file_id = str(raw.get("id") or "")
    identity = item_identity(id_space, file_id)
    if raw.get("trashed") is True:
        return DriveChange(item_id=file_id, deleted=True, identity=identity)
    pending = defer_head and head_revision_due(raw)
    return DriveChange(
        item_id=file_id,
        parent_id=parent_of(raw),
        name=str(raw.get("name") or ""),
        is_folder=is_folder(raw),
        size_bytes=size_of(raw),
        mime_type=mime_of(raw),
        content_version=content_version(raw, None if pending else head_revision_id),
        modified_at=parse_time(raw.get("modifiedTime")),
        fs_modified_at=None,
        web_url=str(raw.get("webViewLink") or ""),
        child_count=0,                                     # G3, GE-11: Drive has no child count
        created_at=parse_time(raw.get("createdTime")),
        identity=identity,
        content_identity=content_identity(raw),
        media=media_facts(raw, id_space),
        content_version_pending=pending,
    )


def resolved_change(change: DriveChange, head_revision_id: Optional[str]) -> DriveChange:
    """A pending change with its final key: `r:{head revision id}` when the list was read, else the `m:` fallback it
    already carries (a refused or empty list). The caller passes pending changes only."""
    version = native_content_version(head_revision_id, None) if head_revision_id else change.content_version
    return replace(change, content_version=version, content_version_pending=False)


def to_item(raw: Mapping[str, Any], path: str, id_space: str, drive_id: str,
            head_revision_id: Optional[str] = None) -> DriveItem:
    """A `files` resource as a `DriveItem` at `path` (built by the caller's parent walk, N1); `drive_id` is the
    connection's drive identity (GE-1)."""
    version = content_version(raw, head_revision_id)
    return DriveItem(
        item_id=str(raw.get("id") or ""),
        name=str(raw.get("name") or ""),
        path=path,
        is_folder=is_folder(raw),
        size_bytes=size_of(raw),
        mime_type=mime_of(raw),
        modified_at=parse_time(raw.get("modifiedTime")),
        web_url=str(raw.get("webViewLink") or ""),
        child_count=0,
        identity=item_identity(id_space, str(raw.get("id") or "")),
        drive_identity=drive_id,
        content_version=version,
        content_identity=content_identity(raw),
        media=media_facts(raw, id_space),
        access=access_of(raw),
    )


def drive_root_change(drive_id: str, name: str, id_space: str) -> DriveChange:
    """A member shared drive's root as a folder change with no parent (§3.3, GE-14): its id is the drive id."""
    return DriveChange(item_id=drive_id, parent_id="", name=name, is_folder=True,
                       identity=item_identity(id_space, drive_id))


def drive_root_item(drive_id: str, name: str, id_space: str, connection_drive: str) -> DriveItem:
    """A member shared drive's root as a folder item printed from its own name (G8, G4)."""
    return DriveItem(item_id=drive_id, name=name, path=join_drive_path("", name), is_folder=True,
                     identity=item_identity(id_space, drive_id), drive_identity=connection_drive,
                     access=ItemAccess(can_download=False, can_write_content=False))


# -- errors ----------------------------------------------------------------------------------------------------------


def error_reason(body: str) -> str:
    """The first `errors[].reason` of a Drive error body (an enum such as `userRateLimitExceeded`); "" when absent or
    not one. The body's free text (it can quote a file name) is never returned."""
    try:
        error = (json.loads(body) or {}).get("error") or {}
        errors = error.get("errors") if isinstance(error, dict) else None
        reason = errors[0].get("reason") if isinstance(errors, list) and errors and isinstance(errors[0], dict) else ""
    except (ValueError, AttributeError, TypeError):
        return ""
    reason = str(reason or "")
    return reason if _REASON_RE.fullmatch(reason) else ""


def classify(status: int, reason: str) -> str:
    """The outcome class of a failed call (§3.7): the reason decides first (GE-18), then the status."""
    if reason in _RATE_REASONS:
        return RETRY
    if reason == "dailyLimitExceeded":
        return DAILY_LIMIT
    if reason in _DENIED_REASONS:
        return ACCESS_DENIED
    if reason == "storageQuotaExceeded":
        return STORAGE_FULL
    if reason == "myDriveHierarchyDepthLimitExceeded":
        return PATH_TOO_LONG
    if reason == "exportSizeLimitExceeded":
        return EXPORT_TOO_LARGE
    if status == 429 or status >= 500:
        return RETRY
    if status == 401:
        return UNAUTHORIZED
    if status == 404:
        return NOT_FOUND
    if status == 400:
        return BAD_REQUEST
    return OTHER


# -- queries and links -----------------------------------------------------------------------------------------------


def escape_query(value: str) -> str:
    """A value inside a single-quoted Drive query string: `\\` and `'` escaped with `\\` [DOC search]."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def sized_thumbnail(link: str, max_edge_px: int) -> str:
    """`link` with its `=s<n>` size suffix set to `max_edge_px` (appended when absent) [PROBE P-f]."""
    if _THUMB_SIZE_RE.search(link):
        return _THUMB_SIZE_RE.sub(f"=s{max_edge_px}", link)
    return f"{link}=s{max_edge_px}"


def split_name(name: str) -> Tuple[str, str]:
    """(stem, extension) for the `name (n).ext` rename (§3.6); a leading-dot name has no extension."""
    dot = name.rfind(".")
    if dot <= 0:
        return name, ""
    return name[:dot], name[dot:]

"""
Card text and its hash (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §8.5). Pure over record fields:
`name | kind | key facets`; no path, so a folder rename touches no descendant.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, List, Mapping, Optional, Tuple

from .drive_index import CARD_VERSION, DriveChange, DriveIndexRecord, Reason, RecordState, Scope
from .drive_index_policy import is_generated_dir, searchable

_KIND_LABEL = {
    "pdf": "PDF document", "docx": "Word document", "doc": "Word document", "pptx": "presentation",
    "ppt": "presentation", "xlsx": "spreadsheet", "xls": "spreadsheet", "ods": "spreadsheet", "csv": "table (CSV)",
    "odt": "text document", "odp": "presentation", "epub": "e-book", "html": "web page", "mhtml": "saved web page",
    "markdown": "note (Markdown)", "text": "text file", "json": "data (JSON)", "rtf": "text document",
    "msg": "email message", "eml": "email message", "subtitles": "transcript / subtitles", "iwork": "Apple iWork file",
    "image": "image", "audio": "audio", "video": "video", "archive": "archive", "code": "source code",
    "log": "log file", "data_dump": "data dump", "database": "database", "executable": "program",
    "disk_image": "disk image", "other": "file", "folder": "folder",
}


def _parse_dt(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # an offset-less timestamp is read as UTC, so it compares with the aware datetimes stored
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def photo_location(facets: Mapping[str, Any]) -> Optional[Tuple[float, float]]:
    loc = facets.get("location") or {}
    lat, lon = loc.get("latitude"), loc.get("longitude")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        return float(lat), float(lon)
    return None


def effective_date(change: DriveChange, fallback: datetime) -> datetime:
    """The date a user means (§3.1): taken, else filesystem-modified, else server time."""
    taken = _parse_dt((change.facets.get("photo") or {}).get("takenDateTime"))
    return taken or change.fs_modified_at or change.modified_at or fallback


def content_hash_of(change: DriveChange) -> Optional[str]:
    qx = (change.facets.get("hashes") or {}).get("quickXorHash")
    return f"{qx}:{change.size_bytes}" if qx else None


def _duration(ms: Any) -> Optional[str]:
    if not isinstance(ms, (int, float)) or ms <= 0:
        return None
    total = int(ms // 1000)
    return f"{total // 60}:{total % 60:02d}"


def _key_facets(rec: DriveIndexRecord) -> List[str]:
    out: List[str] = []
    photo = rec.facets.get("photo") or {}
    taken = _parse_dt(photo.get("takenDateTime"))
    if taken:
        out.append(f"taken {taken.date().isoformat()}")
    camera = " ".join(p for p in (photo.get("cameraMake"), photo.get("cameraModel")) if p)
    if camera:
        out.append(f"camera {camera}")
    if rec.place_name:
        out.append(f"place {rec.place_name}")
    image = rec.facets.get("image") or {}
    if image.get("width") and image.get("height"):
        out.append(f"{image['width']}x{image['height']}")
    video = rec.facets.get("video") or {}
    duration = _duration(video.get("duration"))
    if duration:
        dims = f" {video['width']}x{video['height']}" if video.get("width") and video.get("height") else ""
        out.append(f"video {duration}{dims}")
    return out


# A dense folder's count in buckets: its card text, and so its embedding, holds while children come and go.
_COUNT_BUCKETS = (100_000, 50_000, 10_000, 5_000, 1_000, 500, 100)


def _count_bucket(count: int) -> Optional[str]:
    return next((f"{b}+" for b in _COUNT_BUCKETS if count >= b), None)


def _opaque_label(rec: DriveIndexRecord) -> str:
    if rec.reason is Reason.DOT_NAME:
        return "hidden folder"
    if rec.reason is Reason.DENSE_TREE:
        bucket = _count_bucket(rec.child_count)
        return f"generated files ({bucket} items)" if bucket else "generated files"
    return "generated files" if is_generated_dir(rec.name) else "repository"


def card_text(rec: DriveIndexRecord) -> str:
    if rec.is_folder:
        if rec.scope is Scope.OPAQUE_ROOT:
            return f"{rec.name} | {_opaque_label(rec)}"
        return f"{rec.name} | folder"
    return " | ".join([rec.name, _KIND_LABEL.get(rec.kind, "file"), *_key_facets(rec)])


def card_hash(rec: DriveIndexRecord) -> str:
    return hashlib.sha256(f"{CARD_VERSION}\n{card_text(rec)}".encode("utf-8")).hexdigest()[:32]


def card_eligible(rec: DriveIndexRecord) -> bool:
    """A record carries a card unless it is unresolved, inside an opaque root, deleting or capped (§3.4, §5.2)."""
    return structurally_card_eligible(rec) and not rec.card_capped


def structurally_card_eligible(rec: DriveIndexRecord) -> bool:
    """What the structure and the policy allow, before the card cap (§5.4) has its say."""
    return (rec.scope in (Scope.NORMAL, Scope.OPAQUE_ROOT)
            and rec.state not in (RecordState.UNRESOLVED, RecordState.DELETING) and searchable(rec.reason))

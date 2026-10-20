"""
Card text and its hash (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §8.5, §25.2.7). Pure over record fields:
`name | kind | ... | key facets`; no path, so a folder rename touches no descendant. CARD_VERSION 3 carries the
profile and the photo fields; no lead text but for machine text, so card kNN and chunk kNN no longer vote on one signal.
CARD_VERSION 4 adds the capture (§26.3) and drops the brief's doc type (Δ64), and a photograph's scene labels (D40).
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, List, Mapping, Optional, Tuple

from .drive_capture import CAPTURE_LABELS, Capture
from .drive_index import (
    CARD_VERSION, DocumentBrief, DriveChange, DriveIndexRecord, Reason, RecordState, Scope, TriageVerdict,
)
from .drive_labels import scene_label_text
from .drive_index_policy import TRANSCRIBED_KINDS, is_generated_dir, searchable
from .drive_transcript_render import card_segment

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


def coarse_location(facets: Mapping[str, Any]) -> Optional[Tuple[float, float]]:
    """A photo's location rounded to 3 decimals (~110 m): the only precision a model is ever sent (D39)."""
    loc = photo_location(facets)
    return None if loc is None else (round(loc[0], 3), round(loc[1], 3))


def taken_at(facets: Mapping[str, Any]) -> Optional[datetime]:
    """A photo's taken date, from its `photo` facet."""
    return _parse_dt((facets.get("photo") or {}).get("takenDateTime"))


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


def _brief_segments(brief: Optional[DocumentBrief]) -> List[str]:
    """The reader's brief: its doc type never (Δ64: the profile's is the card's only doc type; reader 2 returns
    none, a v1 brief may still hold one)."""
    if brief is None:
        return []
    out = [brief.description] if brief.description else []
    if brief.doc_date is not None:
        out.append(brief.doc_date.date().isoformat())
    if brief.parties:
        out.append("parties: " + ", ".join(brief.parties))
    if brief.amount:
        out.append(brief.amount)
    return out


def repository_root(rec: DriveIndexRecord) -> bool:
    """A folder made opaque by a marker child (`.git`, `pyvenv.cfg`): its card reads `repository` (§5.2, RFC Δ33)."""
    return (rec.is_folder and rec.scope is Scope.OPAQUE_ROOT and rec.reason is Reason.OPAQUE_ROOT
            and not is_generated_dir(rec.name))


def collapsed_folder(rec: DriveIndexRecord) -> bool:
    """A folder kept as one record (any opaque_root reason: repository, generated, hidden, dense): it gets a profile
    from its README (§25.3)."""
    return rec.is_folder and rec.scope is Scope.OPAQUE_ROOT


_MACHINE_TEXT = "machine-generated text"


def _capture_segment(rec: DriveIndexRecord) -> str:
    """§26.3: how the file was captured, in words; none for `digital` (it would only shift every native file's
    embedding) and none without a capture."""
    if not rec.capture or rec.capture == Capture.DIGITAL.value:
        return ""
    return CAPTURE_LABELS.get(rec.capture, "")


def _joined(parts: List[str]) -> str:
    return " | ".join(p for p in parts if p)


def _transcript_segment(rec: DriveIndexRecord) -> str:
    """§27.8 (a): the transcript line of a transcribed media record; none for every other kind and for a media
    record without a transcript, so no existing card's text (and no `card_hash`) changes (G5)."""
    if rec.kind not in TRANSCRIBED_KINDS:
        return ""
    return card_segment(rec) or ""


def card_text(rec: DriveIndexRecord) -> str:
    """§25.2.7 (CARD_VERSION 4: §26.3's capture), each segment only when present:
    - profiled file: `name | kind | capture | doc_type | what_it_is | title | entities (as printed) | topics |
      topics_native | facets`;
    - text file without a profile: `name | kind | capture | brief (no doc type, Δ64) | facets`;
    - machine text: `name | kind | machine-generated text | lead text`;
    - photograph: `name | image | description | scene | scene labels | objects | visible text | facets`;
    - transcribed audio/video: the same branches, each with the transcript line (`drive_transcript_render.card_segment`)
      appended (§27.8 a, no CARD_VERSION bump, G5);
    - collapsed folder with a README profile: `name | label | what_it_is | title | topics`; without, its Slice 1
      card."""
    if rec.is_folder:
        if rec.scope is Scope.OPAQUE_ROOT:
            p = rec.profile
            extra = [p.what_it_is, p.title, ", ".join(p.topics)] if p is not None else []
            return _joined([rec.name, _opaque_label(rec), *extra])
        return f"{rec.name} | folder"
    kind = _KIND_LABEL.get(rec.kind, "file")
    if rec.reason is Reason.MACHINE_TEXT:
        return _joined([rec.name, kind, _MACHINE_TEXT, rec.lead_text, _transcript_segment(rec)])
    if rec.triage_verdict is TriageVerdict.PHOTO and rec.state is not RecordState.PENDING:
        return _joined([rec.name, kind, rec.photo_description, rec.photo_scene,
                        ", ".join(scene_label_text(label) for label in rec.photo_scene_labels),
                        ", ".join(o.name for o in rec.photo_objects if o.name), rec.visible_text, *_key_facets(rec)])
    p = rec.profile
    if p is not None:
        # an entity as the file prints it, never only the profiler's spelling of it (xc F1)
        return _joined([rec.name, kind, _capture_segment(rec), p.doc_type, p.what_it_is, p.title,
                        ", ".join(e.printed or e.name for e in p.entities if e.printed or e.name),
                        ", ".join(p.topics),
                        ", ".join(p.topics_native), *_key_facets(rec), _transcript_segment(rec)])
    return _joined([rec.name, kind, _capture_segment(rec), *_brief_segments(rec.brief), *_key_facets(rec),
                    _transcript_segment(rec)])


def card_hash(rec: DriveIndexRecord) -> str:
    return hashlib.sha256(f"{CARD_VERSION}\n{card_text(rec)}".encode("utf-8")).hexdigest()[:32]


def card_eligible(rec: DriveIndexRecord) -> bool:
    """A record carries a card unless it is unresolved, inside an opaque root, deleting or capped (§3.4, §5.2)."""
    return structurally_card_eligible(rec) and not rec.card_capped


def structurally_card_eligible(rec: DriveIndexRecord) -> bool:
    """What the structure and the policy allow, before the card cap (§5.4) has its say."""
    return (rec.scope in (Scope.NORMAL, Scope.OPAQUE_ROOT)
            and rec.state not in (RecordState.UNRESOLVED, RecordState.DELETING) and searchable(rec.reason))

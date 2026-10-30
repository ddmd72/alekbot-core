"""
Card text and its hash (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §8.5, §25.2.7). Pure over record fields:
`name | kind | ... | key facets`; no path, so a folder rename touches no descendant. CARD_VERSION 3 carries the
profile and the photo fields; no lead text but for machine text, so card kNN and chunk kNN no longer vote on one signal.
CARD_VERSION 4 adds the capture (§26.3) and drops the brief's doc type (Δ64), and a photograph's scene labels (D40).

The owner's note (DRIVE_FILE_NOTES_RFC §5.3, D7) is a segment right after `name | kind` in every branch, with no
CARD_VERSION bump (the G5 precedent): `_joined` drops an empty segment, so a record without a note keeps a byte-identical
card text and hash. `note_line(rec)` is the same note as the search card renders it, on its own line.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import List, Optional, Tuple

from .drive_capture import CAPTURE_LABELS, Capture
from .drive_index import (
    CARD_VERSION, DocumentBrief, DriveChange, DriveIndexRecord, Reason, RecordState, Scope, TriageVerdict,
)
from .drive_content_identity import ContentIdentity
from .drive_labels import scene_label_text
from .drive_media_facts import DriveMediaFacts
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


def photo_location(media: DriveMediaFacts) -> Optional[Tuple[float, float]]:
    """Where a photo was taken, from the typed media facts (MULTI_DRIVE_RFC §7.2); None when the provider gave none."""
    return media.location


def coarse_location(media: DriveMediaFacts) -> Optional[Tuple[float, float]]:
    """A photo's location rounded to 3 decimals (~110 m): the only precision a model is ever sent (D39)."""
    loc = photo_location(media)
    return None if loc is None else (round(loc[0], 3), round(loc[1], 3))


def taken_at(media: DriveMediaFacts) -> Optional[datetime]:
    """A photo's taken date (an aware datetime the adapter parsed), None when unknown."""
    return media.taken_at


def content_key_of(identity: Optional[ContentIdentity]) -> Optional[str]:
    """The rendered content identity `{family}:{digest}:{size}` (MULTI_DRIVE_RFC §7.2): two copies of one file share
    it, on any connection (the duplicate collapse, the transcript store); None when the provider gave no hash."""
    return identity.render() if identity is not None else None


def effective_date(change: DriveChange, fallback: datetime) -> datetime:
    """The date a user means (§3.1): taken, else filesystem-modified, else server time."""
    return change.media.taken_at or change.fs_modified_at or change.modified_at or fallback


def _duration(ms: int) -> Optional[str]:
    if ms <= 0:
        return None
    total = int(ms // 1000)
    return f"{total // 60}:{total % 60:02d}"


def _key_facets(rec: DriveIndexRecord) -> List[str]:
    """The typed media facts a card shows (MULTI_DRIVE_RFC §7.2): taken date, camera, place, image size, video length
    and frame size."""
    out: List[str] = []
    media = rec.media
    if media.taken_at:
        out.append(f"taken {media.taken_at.date().isoformat()}")
    camera = " ".join(p for p in (media.camera_make, media.camera_model) if p)
    if camera:
        out.append(f"camera {camera}")
    if rec.place_name:
        out.append(f"place {rec.place_name}")
    if media.image_width and media.image_height:
        out.append(f"{media.image_width}x{media.image_height}")
    duration = _duration(media.video_duration_ms)
    if duration:
        dims = f" {media.video_width}x{media.video_height}" if media.video_width and media.video_height else ""
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


def _label(value: str) -> str:
    """A label value as words ("bank_statement" -> "bank statement"), as the deleted-files lines show it (part C)."""
    return value.replace("_", " ")


def _owner_parts(text: str, doc_type: str, life_areas: Tuple[str, ...]) -> List[str]:
    out = [text] if text else []
    if doc_type:
        out.append(f"type {_label(doc_type)}")
    if life_areas:
        out.append("life areas " + ", ".join(_label(a) for a in life_areas))
    return out


def owner_segment(rec: DriveIndexRecord) -> str:
    """DRIVE_FILE_NOTES_RFC §5.3: `noted by the owner: <text>; type <doc_type>; life areas <a>, <b>`, each part only when
    present (a classifier-only note reads `noted by the owner: type contract`, never an empty quote); "" without a note.
    The owner's words outrank the model's, so the segment sits right after `name | kind`."""
    parts = _owner_parts(rec.owner_note_text, rec.owner_doc_type, tuple(rec.owner_life_areas))
    return "noted by the owner: " + "; ".join(parts) if parts else ""


def owner_note_line(text: str, doc_type: str, life_areas: Tuple[str, ...]) -> str:
    """The rendered note (§6.2): `noted by the owner: «<text>» (type contract; life areas home)`, or `noted by the owner:
    type contract` for a classifier-only note; "" for no note. One renderer for search cards and the open header."""
    labels = _owner_parts("", doc_type, tuple(life_areas))
    if text:
        return f"noted by the owner: «{text}»" + (f" ({'; '.join(labels)})" if labels else "")
    return "noted by the owner: " + "; ".join(labels) if labels else ""


def note_line(rec: DriveIndexRecord) -> str:
    """`owner_note_line` of the record's copy of the note ("" for a record without one)."""
    return owner_note_line(rec.owner_note_text, rec.owner_doc_type, tuple(rec.owner_life_areas))


def _transcript_segment(rec: DriveIndexRecord) -> str:
    """§27.8 (a): the transcript line of a transcribed media record; none for every other kind and for a media
    record without a transcript, so no existing card's text (and no `card_hash`) changes (G5)."""
    if rec.kind not in TRANSCRIBED_KINDS:
        return ""
    return card_segment(rec) or ""


def card_text(rec: DriveIndexRecord, *, owner: bool = True) -> str:
    """§25.2.7 (CARD_VERSION 4: §26.3's capture), each segment only when present; the owner's note segment
    (`owner_segment`, DRIVE_FILE_NOTES_RFC §5.3) right after `name | kind` (a collapsed folder: after its label) in every
    branch, and a profiled file's doc type left out when the owner gave one (never shown twice). `owner=False` leaves
    the segment out: the search card renders the note on its own line (§6.2), the card hash and embedding keep it.
    - profiled file: `name | kind | capture | doc_type | what_it_is | title | entities (as printed) | topics |
      topics_native | facets`;
    - text file without a profile: `name | kind | capture | brief (no doc type, Δ64) | facets`;
    - machine text: `name | kind | machine-generated text | lead text`;
    - photograph: `name | image | description | scene | scene labels | objects | visible text | facets`;
    - transcribed audio/video: the same branches, each with the transcript line (`drive_transcript_render.card_segment`)
      appended (§27.8 a, no CARD_VERSION bump, G5);
    - collapsed folder with a README profile: `name | label | what_it_is | title | topics`; without, its Slice 1
      card."""
    seg = owner_segment(rec) if owner else ""
    if rec.is_folder:
        if rec.scope is Scope.OPAQUE_ROOT:
            p = rec.profile
            extra = [p.what_it_is, p.title, ", ".join(p.topics)] if p is not None else []
            return _joined([rec.name, _opaque_label(rec), seg, *extra])
        # the plain folder: the exact Slice 1 text without a note (an empty name included), the segment after it
        return f"{rec.name} | folder | {seg}" if seg else f"{rec.name} | folder"
    kind = _KIND_LABEL.get(rec.kind, "file")
    if rec.reason is Reason.MACHINE_TEXT:
        return _joined([rec.name, kind, seg, _MACHINE_TEXT, rec.lead_text, _transcript_segment(rec)])
    if rec.triage_verdict is TriageVerdict.PHOTO and rec.state is not RecordState.PENDING:
        return _joined([rec.name, kind, seg, rec.photo_description, rec.photo_scene,
                        ", ".join(scene_label_text(label) for label in rec.photo_scene_labels),
                        ", ".join(o.name for o in rec.photo_objects if o.name), rec.visible_text, *_key_facets(rec)])
    p = rec.profile
    if p is not None:
        # an entity as the file prints it, never only the profiler's spelling of it (xc F1); the profile's doc type
        # only when the owner gave none (D7: the owner's type is in the segment, and the stored profile is untouched)
        return _joined([rec.name, kind, seg, _capture_segment(rec), "" if rec.owner_doc_type else p.doc_type,
                        p.what_it_is, p.title,
                        ", ".join(e.printed or e.name for e in p.entities if e.printed or e.name),
                        ", ".join(p.topics),
                        ", ".join(p.topics_native), *_key_facets(rec), _transcript_segment(rec)])
    return _joined([rec.name, kind, seg, _capture_segment(rec), *_brief_segments(rec.brief), *_key_facets(rec),
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

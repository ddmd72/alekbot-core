"""
Index-run rules around the planner's transitions (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §8.1, §8.2, §5.3, §5.4,
§24.5, §24.8). Pure: the tick asks these what to do with an outcome, an image or the chunk cap.

Public interface:
    RunStep, outcome_step(status) -> Tuple[RunStep, Optional[Reason]]
    gate_exempt(kind) -> bool                     tables skip the text-quality gate (§8.3)
    routed(kind) -> bool                          the kind may reach the reader (§24.3 deadline rule)
    camera_prior(facets) -> bool                  the triage hint (§5.3)
    original_fallback(rec) -> bool               the reader may get the original image (§24.5, S0g)
    image_mime(head) -> str                       a provider rendition is JPEG or PNG (S0f)
    file_suffix(name) -> str                      a safe extension for the temp file
    chunk_cap_refuses(readable_total, rec, new_chunks) -> bool
    releasable(capped, headroom) -> List[DriveIndexRecord]
    chunk_released(rec, now) -> DriveIndexRecord
    readme_choice(children) -> Optional[DriveChange]  the README a repository card carries (§5.2)
"""
from __future__ import annotations

import os
import re
from dataclasses import replace
from datetime import datetime
from enum import Enum
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from .drive_extraction import READER, ExtractionStatus, chain_for
from .drive_image_mime import MAX_VISION_IMAGE_BYTES
from .drive_index import DriveChange, DriveIndexRecord, Priority, Reason, RecordState
from .drive_index_params import MAX_CHUNKS_PER_USER
from .drive_index_planner import schedule
from .drive_index_policy import is_readme_name

READER_IMAGE_MAX_BYTES = MAX_VISION_IMAGE_BYTES   # the vision input cap the original may be sent under (§24.5)
README_MAX_BYTES = 1024 * 1024              # a README is read for its lead text only (§5.2)
_README_ORDER = (".md", ".markdown", ".txt", ".rst", "")
_TABLE_KINDS = frozenset({"csv", "xlsx", "xls", "ods"})
_READABLE_ORIGINALS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})
_READABLE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
_SUFFIX = re.compile(r"\.[a-z0-9]{1,8}")


class RunStep(str, Enum):
    TEXT = "text"              # go on: gate, chunks, embeddings, commit
    DEMOTE = "demote"          # deterministic: metadata_only with the reason (§8.2)
    RETRY = "retry"            # per-file transient: back off, `failed` at MAX_ATTEMPTS (§8.2)
    WAIT_MONTH = "wait_month"  # the reader's monthly pages are spent: pending in class 3 until next month (§24.7)


_STEPS = {
    ExtractionStatus.TEXT: (RunStep.TEXT, None),
    ExtractionStatus.NO_TEXT: (RunStep.DEMOTE, Reason.NO_TEXT),
    ExtractionStatus.UNSUPPORTED: (RunStep.DEMOTE, Reason.KIND_NOT_ADMITTED),
    ExtractionStatus.TOO_LARGE: (RunStep.DEMOTE, Reason.TOO_LARGE),
    ExtractionStatus.ENCRYPTED: (RunStep.DEMOTE, Reason.ENCRYPTED),
    ExtractionStatus.TIMEOUT: (RunStep.RETRY, Reason.EXTRACT_TIMEOUT),
    ExtractionStatus.MEMORY: (RunStep.RETRY, Reason.EXTRACT_MEMORY),
    ExtractionStatus.ERROR: (RunStep.RETRY, Reason.EXTRACT_ERROR),
    ExtractionStatus.BUDGET_EXHAUSTED: (RunStep.WAIT_MONTH, None),
}


def outcome_step(status: ExtractionStatus) -> Tuple[RunStep, Optional[Reason]]:
    """What the final link's outcome means for the record (§8.2, §24.8)."""
    return _STEPS[status]


def gate_exempt(kind: str) -> bool:
    """Tables are row-capped and treated as tables, never as machine text (§5.5 class 3, §8.3)."""
    return kind in _TABLE_KINDS


def routed(kind: str) -> bool:
    """The kind's chain reaches the reader, so its worst case includes MODEL_FILE_BUDGET (§24.3)."""
    return READER in chain_for(kind)


def camera_prior(facets: Mapping[str, Any]) -> bool:
    """`photo.takenDateTime` or `photo.cameraMake`, never the `photo` key alone (S0e saw empty `photo: {}`)."""
    photo = facets.get("photo") or {}
    return isinstance(photo, Mapping) and bool(photo.get("takenDateTime") or photo.get("cameraMake"))


def original_fallback(rec: DriveIndexRecord) -> bool:
    """With no provider rendition, the original goes to the reader only as JPEG/PNG/WebP/GIF within the vision cap
    (§24.5); anything else (HEIC, RAW, TIFF) is never decoded by us."""
    ext = os.path.splitext(rec.name)[1].lower()
    readable = ext in _READABLE_ORIGINALS or rec.mime_type.lower() in _READABLE_MIMES
    return readable and 0 < rec.size_bytes <= READER_IMAGE_MAX_BYTES


def image_mime(head: bytes) -> str:
    """Renditions are served as JPEG or PNG (S0f; HEIC as JPEG, SVG as PNG)."""
    return "image/png" if head.startswith(b"\x89PNG") else "image/jpeg"


def file_suffix(name: str) -> str:
    """The item's extension when it is plain (converters dispatch on it), else none: a file name is untrusted."""
    ext = os.path.splitext(name)[1].lower()
    return ext if _SUFFIX.fullmatch(ext) else ""


def chunk_cap_refuses(readable_total: int, rec: DriveIndexRecord, new_chunks: int) -> bool:
    """§5.4: the file's chunks would push the user's readable chunks past MAX_CHUNKS_PER_USER. The generation it
    replaces stops counting when this one commits."""
    own = rec.chunk_count if rec.indexed_generation is not None else 0
    return readable_total - own + new_chunks > MAX_CHUNKS_PER_USER


def releasable(capped: Sequence[DriveIndexRecord], headroom: int) -> List[DriveIndexRecord]:
    """§5.4 release: in the given order (best rank first, then newest), each refused file whose recorded
    `chunk_count` fits in what headroom is left; a file that does not fit does not block a smaller one after it."""
    out: List[DriveIndexRecord] = []
    for rec in capped:
        if rec.state is RecordState.METADATA_ONLY and rec.reason is Reason.USER_CHUNK_CAP \
                and rec.chunk_count <= headroom:
            out.append(rec)
            headroom -= rec.chunk_count
    return out


def chunk_released(rec: DriveIndexRecord, now: datetime) -> DriveIndexRecord:
    """A refused file fits again: pending, its run due in class 3 (§5.4, §7.8)."""
    pending = schedule(replace(rec, state=RecordState.PENDING, reason=None), now, first_seen=False)
    return pending if pending.due_at is None else replace(pending, priority=int(Priority.MAINTENANCE))


def readme_choice(children: Sequence[DriveChange]) -> Optional[DriveChange]:
    """Among a repository root's children, the README its card carries (§5.2), chosen deterministically: Markdown,
    then text, then the rest; then the shortest name; then the item id."""
    found = [c for c in children if not c.deleted and not c.is_folder and is_readme_name(c.name)]
    if not found:
        return None

    def rank(change: DriveChange) -> Tuple[int, int, str]:
        ext = os.path.splitext(change.name)[1].lower()
        order = _README_ORDER.index(ext) if ext in _README_ORDER else len(_README_ORDER)
        return order, len(change.name), change.item_id
    return min(found, key=rank)

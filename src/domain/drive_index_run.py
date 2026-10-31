"""
Index-run rules around the planner's transitions (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §8.1, §8.2, §5.3, §5.4,
§24.5, §24.8). Pure: the tick asks these what to do with an outcome, an image or the chunk cap.

Public interface:
    RunStep, outcome_step(status) -> Tuple[RunStep, Optional[Reason]]
    gate_exempt(kind) -> bool                     tables skip the text-quality gate (§8.3)
    routed(kind) -> bool                          the kind may reach the reader (§24.3 deadline rule)
    reread_cooldown_until(rec) -> Optional[datetime]  the reader's next read of an edit of a file it read (F1-3)
    camera_prior(media) -> bool                   the triage hint (§5.3)
    original_fallback(rec) -> bool               the reader may get the original image (§24.5, S0g)
    image_mime(head) -> str                       a provider rendition is JPEG or PNG (S0f)
    file_suffix(name) -> str                      a safe extension for the temp file
    chunk_cap_refuses(readable_total, rec, new_chunks) -> bool
    releasable(capped, headroom) -> List[DriveIndexRecord]
    chunk_released(rec, now) -> DriveIndexRecord
    readme_choice(children) -> Optional[DriveChange]  the README a repository card carries (§5.2)
    reader_pages(kind, parsed) -> int             pages a file needs from the month's reader budget (§24.7)
    no_rendition(rec, now) -> DriveIndexRecord    an image without a provider rendition (§24.5)
"""
from __future__ import annotations

import os
import re
from dataclasses import replace
from datetime import datetime
from enum import Enum
from typing import List, Optional, Sequence, Tuple

from .drive_extraction import READER, ExtractionOutcome, ExtractionStatus, TriageResult, chain_for
from .drive_image_mime import MAX_VISION_IMAGE_BYTES
from .drive_media_facts import DriveMediaFacts
from .drive_index import DriveChange, DriveIndexRecord, Priority, Reason, RecordState, TriageVerdict
from .drive_index_params import (
    MAX_ATTEMPTS, MAX_CHUNKS_PER_USER, MODEL_EXTRACT_MAX_PAGES, MODEL_EXTRACT_PAGES_PER_MONTH, MODEL_REREAD_COOLDOWN,
)
from .drive_index_planner import content_retry, schedule, triaged
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


def reread_cooldown_until(rec: DriveIndexRecord) -> Optional[datetime]:
    """When the reader may read an edit of this file (final review F1-3): MODEL_REREAD_COOLDOWN after it last settled
    the file's content. None — no wait — for a file the reader never read, and for work that is not an edit (first
    sight, a nudge, an upgrade or a release is not dirty, §7.3): a new reader version still re-reads at once."""
    if rec.model_read_at is None or rec.dirty_since is None:
        return None
    return rec.model_read_at + MODEL_REREAD_COOLDOWN


def routed(kind: str) -> bool:
    """The kind's chain reaches the reader, so its worst case includes MODEL_FILE_BUDGET (§24.3)."""
    return READER in chain_for(kind)


def camera_prior(media: DriveMediaFacts) -> bool:
    """A taken date or a camera make in the typed media facts (MULTI_DRIVE_RFC §7.2); an empty photo facet sets
    neither (S0e)."""
    return media.taken_at is not None or bool(media.camera_make)


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


def reader_pages(kind: str, parsed: Optional[ExtractionOutcome]) -> int:
    """Pages the reader needs from the dollars left for the file (§24.7, §25.4.5), known before it is called: an image
    is one page; a PDF is the page count the parser's outcome carries, at most MODEL_EXTRACT_MAX_PAGES, and that maximum
    when the parser could not count. A file waits for the next month rather than being cut by what is left of this
    one; only a file that needs more than the whole monthly reader cap pays (MODEL_EXTRACT_PAGES_PER_MONTH, derived
    from READER_USD_PER_MONTH) is cut to it, so no file waits for a month that could never pay for it."""
    if kind != "pdf":
        return 1
    pages = parsed.pages_total if parsed is not None and parsed.pages_total > 0 else MODEL_EXTRACT_MAX_PAGES
    return min(pages, MODEL_EXTRACT_MAX_PAGES, MODEL_EXTRACT_PAGES_PER_MONTH)


def no_rendition(rec: DriveIndexRecord, now: datetime) -> DriveIndexRecord:
    """The provider has no rendition of an image waiting for triage (§24.5). A missing rendition may be one the
    provider has not made yet: retried with backoff, and a photograph without a caption only at MAX_ATTEMPTS."""
    if rec.attempts + 1 >= MAX_ATTEMPTS:
        return triaged(rec, TriageResult(item_id=rec.item_id, verdict=TriageVerdict.PHOTO), now)
    return content_retry(rec, Reason.DOWNLOAD_ERROR, now)

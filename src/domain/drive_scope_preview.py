"""
What a drive scope addition would put into the index, counted before the owner commits to it (Cabinet folder picker,
decisions/drive_scope_add_preview.md). Pure: the walk lives in UserDriveService.scope_preview, the buckets and the
descent rule here. Both reuse the indexer's own predicates (drive_index_policy), so the count is what the indexer
would take; a rule changed there changes the preview with it.

Several public names live here on purpose (a module waiver of the one-class-per-file rule): the bucket enum, the two
rules and the result type are one vocabulary, and splitting them would only add imports.

Size in bytes is deliberately not counted: a Google-native document has none, and the cost of indexing follows the
text volume and the number of files.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable

from .document_kind import kind_of
from .drive_index import DriveChange
from .drive_index_policy import (
    Admission, admission, is_dot_name, is_generated_dir, is_marker_child, item_rule, size_cap,
)

# A walk past this many files is already an answer ("a lot"); counting the rest only costs provider calls.
PREVIEW_MAX_FILES = 2000
# Bounds the number of listing calls (one per folder at least) the Cabinet request may spend.
PREVIEW_MAX_FOLDERS = 500
# From here the first indexing is a long and paid run, worth a warning line.
PREVIEW_LARGE_FILES = 200


class PreviewBucket(str, Enum):
    DOCUMENT = "document"      # text is extracted and indexed
    IMAGE = "image"            # triaged first; a text verdict admits the content
    RECORDING = "recording"    # audio and video, transcribed
    OTHER = "other"            # metadata only: an excluded name, an unadmitted kind, a file over its download cap


def bucket_of(change: DriveChange) -> PreviewBucket:
    """The bucket of one file, by the indexer's admission rules (`classify`, USER_DRIVE_INDEXING_RFC §5)."""
    if item_rule(change) is not None:
        return PreviewBucket.OTHER
    kind = kind_of(change.name, change.mime_type, change.is_folder)
    adm = admission(kind)
    if adm is Admission.CONTENT:
        return PreviewBucket.DOCUMENT if change.size_bytes <= size_cap(kind) else PreviewBucket.OTHER
    if adm is Admission.TRIAGE:
        return PreviewBucket.IMAGE
    if adm is Admission.TRANSCRIBE:
        return PreviewBucket.RECORDING
    return PreviewBucket.OTHER


def descend_into(change: DriveChange) -> bool:
    """Whether the indexer reads inside this folder: not a dot name, not a tool-generated directory (`classify`)."""
    return change.is_folder and not is_dot_name(change.name) and not is_generated_dir(change.name)


def is_marked_opaque(children: Iterable[DriveChange]) -> bool:
    """A folder holding a repository marker (`.git`, `pyvenv.cfg`) is opaque: nothing in it is indexed (§5.2)."""
    return any(is_marker_child(c.name) for c in children)


@dataclass(frozen=True)
class ScopePreview:
    counts: Dict[PreviewBucket, int] = field(default_factory=dict)
    folders: int = 0      # folders read by the walk, the roots included
    capped: bool = False  # the walk stopped at a bound; the counts are a lower bound

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def large(self) -> bool:
        return self.capped or self.total >= PREVIEW_LARGE_FILES

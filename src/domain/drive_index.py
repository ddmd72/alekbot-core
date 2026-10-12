"""
Drive index — domain model of the user-drive mirror (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §3).

Pure: no I/O, no clock. The drive is the source of truth and this index a disposable mirror (§2).
One record per drive item the sync keeps; chunks (Slice 2) are children of a record.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, IntEnum
from typing import Any, Mapping, Optional, Tuple

POLICY_VERSION = 1
CARD_VERSION = 1
GEO_TABLE_VERSION = 1
NAME_KEYS_VERSION = 1                         # the name-stream keyer (drive_name_keys); a change re-keys (§12.3)
CURRENT_VERSIONS: Mapping[str, int] = {"policy": POLICY_VERSION, "card": CARD_VERSION,
                                       "geo_table": GEO_TABLE_VERSION, "name_keys": NAME_KEYS_VERSION}


class Scope(str, Enum):
    NORMAL = "normal"
    OPAQUE_ROOT = "opaque_root"
    INSIDE_OPAQUE = "inside_opaque"
    UNRESOLVED = "unresolved"


class RecordState(str, Enum):
    UNRESOLVED = "unresolved"
    PENDING = "pending"
    INDEXED = "indexed"
    METADATA_ONLY = "metadata_only"
    FAILED = "failed"
    DELETING = "deleting"


class Reason(str, Enum):
    FOLDER = "folder"
    KIND_NOT_ADMITTED = "kind_not_admitted"
    TOO_LARGE = "too_large"
    OPAQUE_ROOT = "opaque_root"
    DOT_NAME = "dot_name"
    SECRET_NAME = "secret_name"
    OS_ARTIFACT = "os_artifact"
    REMOTE_ITEM = "remote_item"
    PACKAGE = "package"
    MALWARE = "malware"
    NO_TEXT = "no_text"
    MACHINE_TEXT = "machine_text"
    DENSE_TREE = "dense_tree"
    ENCRYPTED = "encrypted"
    USER_CHUNK_CAP = "user_chunk_cap"
    USER_CARD_CAP = "user_card_cap"
    EXTRACT_TIMEOUT = "extract_timeout"
    EXTRACT_MEMORY = "extract_memory"
    EXTRACT_ERROR = "extract_error"
    DOWNLOAD_ERROR = "download_error"
    EMBED_TIMEOUT = "embed_timeout"


class ContentClass(str, Enum):
    """What a file is (§5.2), independent of whether this slice admits it."""
    DOCUMENT = "document"
    MEDIA = "media"
    OTHER = "other"
    FOLDER = "folder"


class Priority(IntEnum):
    """`due_records` order (§7.8)."""
    EDIT = 0
    CASCADE = 1
    FIRST_SEEN = 2
    MAINTENANCE = 3
    RETRY = 4


class EpochPhase(str, Enum):
    IDLE = "idle"
    ENUMERATING = "enumerating"
    SWEEPING = "sweeping"


class ErrorCategory(str, Enum):
    RECONNECT_NEEDED = "reconnect_needed"
    THROTTLED = "throttled"
    PROVIDER_ERROR = "provider_error"
    INTERNAL = "internal"


def record_key(user_id: str, item_id: str) -> str:
    return f"{user_id}:{item_id}"


@dataclass(frozen=True)
class DriveChange:
    """One observation from the provider (§3.1). For a deletion only `item_id` and `deleted` are relied on."""
    item_id: str
    deleted: bool = False
    parent_id: str = ""
    name: str = ""
    is_folder: bool = False
    size_bytes: int = 0
    mime_type: str = ""
    content_version: str = ""                 # cTag; "" for folders
    modified_at: Optional[datetime] = None    # server lastModifiedDateTime — scheduling only
    fs_modified_at: Optional[datetime] = None # fileSystemInfo.lastModifiedDateTime — effective date fallback
    web_url: str = ""
    child_count: int = 0                      # folder.childCount
    facets: Mapping[str, Any] = field(default_factory=dict)  # photo, image, video, location, hashes, remoteItem, package, malware


@dataclass(frozen=True)
class DriveChanges:
    """One page of the change feed, in feed order."""
    changes: Tuple[DriveChange, ...]
    cursor: str              # opaque; pass back to get_changes
    complete: bool           # this page ends the round
    full_enumeration: bool   # this page belongs to an enumeration from scratch (onboarding, 410)
    drive_id: str
    root_item_id: str


@dataclass(frozen=True)
class DriveChildrenPage:
    """One page of a folder listing; `next_cursor` is None on the last page."""
    changes: Tuple[DriveChange, ...]
    next_cursor: Optional[str] = None


@dataclass(frozen=True)
class FolderProbe:
    """The first PROBE_CHILDREN children of a folder (§3.6)."""
    marker_found: bool
    class_counts: Mapping[ContentClass, int]
    sampled: int


@dataclass(frozen=True)
class Nudge:
    """A bot write the tick reads first (§7.1 step 1)."""
    mutation_id: str
    item_ids: Tuple[str, ...]
    attempt: int = 0


@dataclass(frozen=True)
class DriveIndexRecord:
    """One per drive item the sync keeps (§3.2). Content fields stay at their defaults in Slice 1."""
    user_id: str
    item_id: str
    parent_id: str = ""
    name: str = ""
    is_folder: bool = False
    size_bytes: int = 0
    mime_type: str = ""
    web_url: str = ""
    child_count: int = 0
    facets: Mapping[str, Any] = field(default_factory=dict)
    kind: str = ""
    content_class: ContentClass = ContentClass.OTHER
    scope: Scope = Scope.UNRESOLVED
    state: RecordState = RecordState.UNRESOLVED
    reason: Optional[Reason] = None
    content_version: str = ""
    modified_at: Optional[datetime] = None
    fs_modified_at: Optional[datetime] = None    # kept so a re-classification recomputes the same effective date
    effective_date: Optional[datetime] = None
    place_name: Optional[str] = None
    geo_table_version: int = 0
    policy_version: int = 0
    extraction_version: int = 0
    card_version: int = 0
    embedding_space: str = ""
    card_hash: Optional[str] = None
    has_card: bool = False
    card_capped: bool = False                 # no card headroom (§5.4): withholds the card, state and reason stay
    card_refused: bool = False                # the provider refused the text `card_hash` names (§8.2); state stays
    card_rank: int = 3
    name_tokens: Tuple[str, ...] = ()
    name_keys_version: int = 0
    content_hash: Optional[str] = None
    # scheduling (§7.3, §7.8)
    dirty_since: Optional[datetime] = None
    due_at: Optional[datetime] = None
    priority: Optional[int] = None
    attempts: int = 0
    attempt_started_at: Optional[datetime] = None
    # pending structural work (§5.2, §7.6); each flag makes the record due
    probe_pending: bool = False
    rescope_pending: bool = False
    cascade_cursor: Optional[str] = None      # last child item_id handled by a rescope page
    reobserve_pending: bool = False           # this folder's never-stored subtree must be re-listed (§5.2)
    reobserve_cursor: Optional[str] = None    # provider page cursor of an unfinished re-listing
    probed_child_count: int = 0               # childCount at the last density decision (hysteresis)
    # content (Slice 2)
    index_generation: int = 0
    indexed_generation: Optional[int] = None
    indexed_version: Optional[str] = None
    extracted_by: Optional[str] = None
    text_chars: int = 0
    pages_total: int = 0
    chunk_count: int = 0
    truncated: bool = False
    seen_epoch: int = 0

    @property
    def stale(self) -> bool:
        return self.indexed_generation is not None and self.content_version != self.indexed_version


@dataclass(frozen=True)
class DriveSyncState:
    """One per user (§3.5)."""
    user_id: str
    drive_id: str = ""
    root_item_id: str = ""
    cursor: Optional[str] = None
    round_complete: bool = False
    round_started_at: Optional[datetime] = None
    epoch: int = 0
    epoch_phase: EpochPhase = EpochPhase.IDLE
    initial_enumeration_done: bool = False
    purging: bool = False                     # a purge started: it finishes before anything else (review A M1)
    created_at: Optional[datetime] = None     # first tick; the staleness baseline before any success (§12.1)
    lease_owner: Optional[str] = None
    lease_until: Optional[datetime] = None
    continuation_enqueued_at: Optional[datetime] = None
    applied_versions: Mapping[str, int] = field(default_factory=dict)
    target_versions: Mapping[str, int] = field(default_factory=dict)
    upgrade_cursor: Optional[str] = None
    upgrade_found_stale: bool = False
    observed_as_of: Optional[datetime] = None
    access_lost_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    last_error: Optional[ErrorCategory] = None
    systemic_failures: int = 0                # consecutive ticks ended by a systemic error (backoff, §8.2)
    stale_alerted: bool = False
    reconnect_notified: bool = False          # plan Δ6
    counters: Mapping[str, int] = field(default_factory=dict)

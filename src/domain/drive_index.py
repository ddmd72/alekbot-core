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

POLICY_VERSION = 2                            # 2: the Slice 2+4 kinds admitted (§24.1)
CARD_VERSION = 2                              # 2: brief, caption and lead text in the card (§24.4)
GEO_TABLE_VERSION = 1
NAME_KEYS_VERSION = 1                         # the name-stream keyer (drive_name_keys); a change re-keys (§12.3)
EXTRACTION_VERSION = 3                        # chunker, text gate, content_terms normaliser (§12.3, §24.6); 2: maximal query forms (D-M1); 3: written-out dates (F5-5)
TRIAGE_VERSION = 1                            # image triage prompt and verdict rules (§5.3, §24.5)
CURRENT_VERSIONS: Mapping[str, int] = {"policy": POLICY_VERSION, "card": CARD_VERSION,
                                       "geo_table": GEO_TABLE_VERSION, "name_keys": NAME_KEYS_VERSION,
                                       "extraction": EXTRACTION_VERSION, "triage": TRIAGE_VERSION}

# Search v2 (§25.4.8, frozen in §25.13.2). Defined, NOT yet in CURRENT_VERSIONS: adding them would start an upgrade
# pass over v1 records. P5 adds them behind the migration gate (§25.8: a state whose applied_versions lacks
# MIGRATION_GATE_KEY is awaiting migration and runs no upgrade pass).
KEYS_VERSION = 1                              # drive_keys and the drive_content_terms normaliser: re-keys from stored text
PROFILE_VERSION = 1                           # profiler prompt, schema, input rules, verification
PASSAGES_VERSION = 1                          # doc2query prompt and grounding
SEARCH_V2_VERSIONS: Mapping[str, int] = {"keys": KEYS_VERSION, "profile": PROFILE_VERSION,
                                         "passages": PASSAGES_VERSION}
MIGRATION_GATE_KEY = "keys"


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
    PHOTOGRAPH = "photograph"                 # an image triaged as a photograph (§24.5)


class TriageVerdict(str, Enum):
    """What an image shows (§5.3): text to read, or a photograph to caption."""
    TEXT = "text"
    PHOTO = "photo"


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


class LeaseLost(Exception):
    """A sync-state write by a tick that no longer holds the user's lease (§7.1): refused, nothing written."""


class DriveIndexStoreUnavailable(Exception):
    """The index store failed transiently (a deadline, an outage, contention, a quota): systemic, it ends the tick and
    is never counted against a file (§8.2). The repository adapter maps its backend's transient errors to it."""


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
class DocumentBrief:
    """What the reader says a document is (§24.4, D8). Model output from untrusted content: shown only inside the
    untrusted wrapper. `doc_date` becomes the record's effective date (D13)."""
    doc_type: str = ""
    description: str = ""
    doc_date: Optional[datetime] = None
    parties: Tuple[str, ...] = ()
    amount: str = ""                          # value and currency as one display string, e.g. "1 250.00 EUR"


@dataclass(frozen=True)
class DriveChunk:
    """A child of a record (§3.3). Readable only while `generation == record.indexed_generation` (§7.4)."""
    user_id: str
    item_id: str
    generation: int
    index: int
    text: str
    char_start: int
    char_end: int
    kind: str
    content_terms: Tuple[str, ...] = ()
    # Search v2 (§25.2.2): the chunk's `w:`, `n:`, `m:` keys (at most WORD_KEYS_PER_CHUNK) and the KEYS_VERSION they
    # were made under. Written by P5; until then empty / 0.
    word_keys: Tuple[str, ...] = ()
    keys_version: int = 0


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
    # content work held back (a retry backoff, a started run, a spent budget, a model outage): due no earlier than
    # `defer_until`, in class `defer_priority`, until the work completes or the content changes (§8.2, §24.7, §24.8)
    defer_until: Optional[datetime] = None
    defer_priority: Optional[int] = None
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
    chunk_space: str = ""                     # the embedding space the readable generation's chunks are in (§12.3)
    chunk_reembed_pending: bool = False       # those chunks are in another space: their stored text is embedded again
    chunks_refused_space: str = ""            # the space that refused this content's chunks (HTTP 400): terminal there,
                                              # no re-read; another space gives it a fresh try (R3 F2-3)
    truncated: bool = False
    lead_text: str = ""                       # first CARD_LEAD_CHARS of the extracted text, for the card (§8.5)
    brief: Optional[DocumentBrief] = None     # model-read documents only (§24.4)
    model_outages: int = 0                    # reader outages charged to this content version (F1-1): at MAX_ATTEMPTS
                                              # the file fails; a content outcome or a new version starts it again
    model_read_at: Optional[datetime] = None  # the reader last settled this file's content (F1-3): not again before
                                              # MODEL_REREAD_COOLDOWN after it
    # image triage (§5.3, §24.5); a verdict holds for the content version it was taken on
    triage_verdict: Optional[TriageVerdict] = None
    triage_version: int = 0
    triaged_content_version: str = ""
    caption: str = ""                         # photographs only, at most ten words
    # a repository root's README (§5.2): its lead text is the root's `lead_text`; read again while pending
    readme_pending: bool = False
    readme_item_id: Optional[str] = None
    readme_version: str = ""                  # the README's content_version the lead text was read from
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
    purging: bool = False                     # a purge started: it finishes before anything else (§7.6)
    created_at: Optional[datetime] = None     # first tick; the staleness baseline before any success (§12.1)
    lease_owner: Optional[str] = None
    lease_until: Optional[datetime] = None
    continuation_enqueued_at: Optional[datetime] = None
    applied_versions: Mapping[str, int] = field(default_factory=dict)
    target_versions: Mapping[str, int] = field(default_factory=dict)
    applied_space: str = ""                   # the embedding space every card is in (§12.3); "" before onboarding
    target_space: str = ""                    # the space the running upgrade pass moves cards to
    upgrade_cursor: Optional[str] = None
    upgrade_found_stale: bool = False
    # "YYYY-MM" (R3 F1-5): mid-pass, a record held for next month's budget was seen this month; after a pass that
    # found nothing else stale, the month the pass is parked for — no new pass before it turns or the target changes
    upgrade_parked_month: str = ""
    observed_as_of: Optional[datetime] = None
    access_lost_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    last_error: Optional[ErrorCategory] = None
    systemic_failures: int = 0                # consecutive ticks ended by a systemic error (backoff, §8.2)
    stale_alerted: bool = False
    reconnect_notified: bool = False          # the fan-out told the user once (RFC Δ6)
    counters: Mapping[str, int] = field(default_factory=dict)
    # model work (§24.7, §24.8): an outage defers only model work; usage is counted per calendar month (UTC)
    model_backoff_until: Optional[datetime] = None
    model_failures: int = 0                   # consecutive model outages, for the backoff
    usage_month: str = ""                     # "YYYY-MM" the two counters below belong to
    model_pages_used: int = 0
    triage_images_used: int = 0

    def cards_in_space(self, embedding_space: str) -> bool:
        """The card stream answers queries embedded in `embedding_space`: no card is left in another space."""
        return self.applied_space == embedding_space

"""
Drive index — domain model of the user-drive mirror (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §3).

Pure: no I/O, no clock. The drive is the source of truth and this index a disposable mirror (§2).
One record per drive item the sync keeps; chunks (Slice 2) are children of a record.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, IntEnum
from typing import Mapping, Optional, Tuple

from .connection_scope import ConnectionScope
from .drive_content_identity import ContentIdentity
from .drive_enrichment import DriveProfile, PhotoObject
from .drive_media_facts import DriveMediaFacts
from .drive_note_check import NoteCheck

POLICY_VERSION = 4                            # 2: the Slice 2+4 kinds admitted (§24.1); 3: audio and video admitted
                                              # for transcription (§27.3); re-plans EVERY record (113/113 for the owner), not only media: structure
                                              # decisions stand from STRUCTURE_POLICY_SINCE, no card re-embed; 4: an
                                              # `.epub.pkgf` package is an epub (§5.1), re-planned as one
# The policy version since which a record's structure decisions (density probe, opaque marker, README read) are valid.
# A bump that only changes media admission must not re-decide structure for every user, so these checks compare
# against this, not POLICY_VERSION. Raise it only when a bump changes how folders are classified.
STRUCTURE_POLICY_SINCE = 2
CARD_VERSION = 4                              # 2: brief, caption and lead text in the card (§24.4); 3: profile and
                                              # photo fields, no lead text but for machine text (§25.2.7); 4: the
                                              # capture segment, no brief doc type (§26.3, Δ64); S6 (unreleased)
                                              # adds a photograph's scene labels segment (D40)
# 2: alternate names, country names, large place (§25.4.3). Bumped before P5 by D30 (no v1 migration: the rollout
# rebuilds from scratch). P5 MUST write `place_forms` (PlaceIndex.place) together with this stamp: `_place` keeps a
# stored place while the stamp matches, so a record stamped 2 without forms would never get them.
GEO_TABLE_VERSION = 2
NAME_KEYS_VERSION = 2                         # the name-stream keyer; 2: drive_keys.name_keys_v2 (§25.2.4)
TRANSCRIBE_VERSION = 1                        # the transcript pipeline (§27); deliberately not in CURRENT_VERSIONS (Δ122)
EXTRACTION_VERSION = 3                        # chunker, text gate, content_terms normaliser (§12.3, §24.6); 2: maximal query forms (D-M1); 3: written-out dates (F5-5)
TRIAGE_VERSION = 3                            # image triage prompt and verdict rules (§5.3, §24.5); 2: enriched (§25.4.3);
                                              # 3: a text image's capture (§26.3); S6 (unreleased) adds the photo
                                              # location prior (D39) and `scene_labels` (D40)

# Search v2 (§25.4.8, frozen in §25.13.2). In CURRENT_VERSIONS since P5, behind the migration gate (§25.8 as amended
# by D30): a sync state whose applied_versions lacks MIGRATION_GATE_KEY was built by v1 and is never upgraded — it runs
# no tick work until the owner's from-scratch rebuild, whose onboarding stamps every key.
KEYS_VERSION = 4                              # drive_keys and the drive_content_terms normaliser: re-keys from stored text
                                              # 3: entity spellings the file does not print are `ef:` (xc F1), a Latin
                                              # word's skeleton is `xl:`/`exl:` (F3), D31 label names (lw:/dw:)
                                              # 4 (§26.7, unreleased: S3 adds to it): capture names `cp:`/`cpa:`,
                                              # space-separated dates (Δ62), `va:`/`ya:`/`ma:` for values the text
                                              # does not hold, and such entities as `ef:` only (Δ60); S6 adds
                                              # the photograph's scene labels `sl:` (D40, SCENE_NAMES)
PROFILE_VERSION = 2                           # profiler prompt, schema, input rules, verification
                                              # 2: dates carry `printed`, value labels document_number and
                                              # personal_id_number (§26.5 Δ63); grading replaces dropping: every
                                              # value kept with a `graded` evidence grade (§26.4, Δ59)
ACCURACY_VERSION = 2                          # drive_accuracy: the grader's use of the text and the weights (§26.4, Δ61)
                                              # 2 (D37): bilingual/abbreviated/compact month forms, ISO basic dates and
                                              # day+month with the year elsewhere grade `in_text_as`
PASSAGES_VERSION = 1                          # doc2query prompt and grounding
SEARCH_V2_VERSIONS: Mapping[str, int] = {"keys": KEYS_VERSION, "profile": PROFILE_VERSION,
                                         "passages": PASSAGES_VERSION}
# D30 by Δ65 (§26.7): the newest key a from-scratch onboarding stamps. A sync state without it was built before it
# existed: gated (no tick work, search says "being rebuilt", one fan-out alert) until the owner's rebuild purges it.
MIGRATION_GATE_KEY = "accuracy"
CURRENT_VERSIONS: Mapping[str, int] = {"policy": POLICY_VERSION, "card": CARD_VERSION,
                                       "geo_table": GEO_TABLE_VERSION, "name_keys": NAME_KEYS_VERSION,
                                       "extraction": EXTRACTION_VERSION, "triage": TRIAGE_VERSION,
                                       "accuracy": ACCURACY_VERSION, **SEARCH_V2_VERSIONS}


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


def record_doc_id(scope: ConnectionScope, item_id: str) -> str:
    """`{connection_id}:{item_id}`: an index record's document id (MULTI_DRIVE_RFC §8, E8)."""
    return f"{scope.connection_id}:{item_id}"


def chunk_doc_id(scope: ConnectionScope, item_id: str, generation: int, index: int) -> str:
    """`{connection_id}:{item_id}:{generation}:{index}`: a chunk's and a hint's document id (§8)."""
    return f"{scope.connection_id}:{item_id}:{generation}:{index}"


def sync_state_doc_id(scope: ConnectionScope) -> str:
    """`{connection_id}`: the sync state's (and its lease's) document id (§8)."""
    return scope.connection_id


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
    created_at: Optional[datetime] = None     # the provider's creation time
    # neutral shapes (MULTI_DRIVE_RFC §7.2), adapter-built: the item identity, the content identity and the typed media
    # facts (photo, image, video, audio, location, package, malware, alias)
    identity: str = ""                        # `{id_space}:{…}`, unique across providers
    content_identity: Optional[ContentIdentity] = None   # None: the provider gave no hash
    media: DriveMediaFacts = DriveMediaFacts()


@dataclass(frozen=True)
class DriveChanges:
    """One page of the change feed, in feed order."""
    changes: Tuple[DriveChange, ...]
    cursor: str              # opaque; pass back to get_changes
    complete: bool           # this page ends the round
    full_enumeration: bool   # this page belongs to an enumeration from scratch (onboarding, 410)
    root_item_id: str
    drive_identity: str = ""  # the drive's adapter-built identity (MULTI_DRIVE_RFC §7.2)


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
    date_grade: str = ""                      # a `Grade` value of `doc_date` against the stored text (§26.4, Δ60); ""
                                              # until graded (a record stored before it, or not yet written)

    @property
    def held_date(self) -> Optional[datetime]:
        """`doc_date` unless grading found it absent from the text: an unconfirmed date never becomes the file's
        effective date, a `y:`/`m:` key or "(document)" (Δ60). An ungraded brief's date counts."""
        return None if self.date_grade == "not_in_text" else self.doc_date


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
    # MULTI_DRIVE_RFC §8 (E8): the connection the chunk belongs to; its doc id is `chunk_doc_id(scope, …)` and `user_id`
    # stays a stored field a read checks (I3). "" on a chunk written before the field existed.
    connection_id: str = ""


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
    # -- Search v2 (§25.2.1, frozen in §25.13.2): neutral defaults; written from P5 on. States are JobState values,
    # holds HoldReason values, `profile_source` a ProfileSource value. `lead_text` (above) is v2's machine-text lead.
    profile: Optional[DriveProfile] = None
    valid_until: Optional[datetime] = None    # verified dates[valid_until], else dates[due]: 00:00 UTC of that date
    profile_source: Optional[str] = None
    profile_input_fingerprint: Optional[str] = None
    profile_fingerprint: Optional[str] = None
    profile_version: int = 0
    profiled_content_version: str = ""
    profile_state: str = "none"
    profile_defer_until: Optional[datetime] = None
    profile_parked_month: str = ""            # "YYYY-MM"
    profile_hold: Optional[str] = None
    profile_hold_version: str = ""            # "<PROFILE_VERSION>:<profile_input_fingerprint>"
    profile_attempts: int = 0
    passages_state: str = "none"
    passages_defer_until: Optional[datetime] = None
    passages_parked_month: str = ""
    passages_hold: Optional[str] = None       # only "failed"
    passages_hold_version: str = ""           # "<PASSAGES_VERSION>:<indexed_generation>"
    passages_attempts: int = 0
    passages_generation: Optional[int] = None
    passages_next_index: int = 0
    passages_done_at: Optional[datetime] = None
    passages_version: int = 0                 # P5 addition: the PASSAGES_VERSION the generation's hints were made under
    enrich_ready: bool = False               # derived by the planner: any job `ready`
    enrich_wake_at: Optional[datetime] = None # derived: the earliest `*_defer_until` of a `deferred` job
    effective_day: str = ""                   # derived: effective_date as "YYYY-MM-DD" (the queue's order)
    describe_gap: str = ""                    # derived: what the coverage line counts this file as (§25.5.6, XC-3):
                                              # "waiting" / "held" (no description yet), "no_passages", or ""
    keys_pending: bool = False
    keys_version: int = 0
    keys_capped: bool = False                 # a key cap cut this record's or its chunks' keys (§25.2.6: "keys cut")
    about_keys: Tuple[str, ...] = ()          # computed only by about_keys_of(rec) (§25.4.9)
    place_forms: Tuple[str, ...] = ()         # the spellings of place_name its keys were made from
    photo_description: str = ""               # enriched triage, photographs only (§25.4.3); replaces caption
    photo_objects: Tuple[PhotoObject, ...] = ()
    photo_scene: str = ""
    photo_scene_labels: Tuple[str, ...] = ()  # D40: SCENE_LABELS values, plain strings (a rollback reads them back, Δ47)
    visible_text: str = ""
    readme_excerpt: str = ""                  # a collapsed folder's README profile input (§25.3)
    readme_excerpt_version: str = ""          # the README content version it came from
    script_rendered: bool = False             # parser@2 flagged the page script-rendered (§25.3): a template profile;
                                              # copied from ExtractionOutcome.script_rendered by P5
    # -- §26.2 (D33, RFC Δ47): files the bot cannot open. Delta lists the item, every id-addressed read answers 404 (its
    # own permission list lacks the app's entry). Read as a state by `inaccessible(rec)`, never a RecordState value: the
    # content outcome and a readable generation stay, and the rollback tag still parses every record (§25.8)
    inaccessible_since: Optional[datetime] = None        # the marking: unchanged while the item stays closed
    inaccessible_checked_at: Optional[datetime] = None   # the last id read that answered 404 (the retry clock)
    inaccessible_reobserved: bool = False                # delta or a listing saw the item or its parent since that
                                                         # read: the next read is due INACCESSIBLE_RETRY_MIN after it
    # the fan-out's own field (Δ54): the `inaccessible_since` it notified the user of. Never in the tick's record
    # writes (firestore_drive_index_repo.record_to_doc leaves it out); written by set_inaccessible_notified only
    inaccessible_notified_since: Optional[datetime] = None
    # -- §26.3 (D34): how a text-bearing file was captured, a drive_capture.Capture value ("" for none: a photograph, a
    # file not read, an ambiguous PDF no model read), and its source, a CaptureSource value: `detected` by code from
    # the file's structure (keyed `cp:`) or said by a model, `automatic` (`cpa:`). Plain strings, no enum (Δ47's
    # rollback invariant): the rollback tag ignores them.
    capture: str = ""
    capture_source: str = ""
    # -- §26.4 (D35, D36, Δ61): the card's accuracy, 0-100, computed by `drive_accuracy` (None: a photograph or a card
    # with no model-written content to check), the ACCURACY_VERSION it was computed under (0: none), and the reading
    # problems it prices: pages the provider refused, a read served by the fallback model. Plain fields, no enum
    # (Δ47's rollback invariant). The score is never in `card_text`, so a weight change re-scores without a re-embed.
    card_accuracy: Optional[int] = None
    accuracy_version: int = 0
    pages_refused: int = 0
    read_by_fallback: bool = False
    # -- §27 (transcripts): plain fields, strings for states and holds (no enum, Δ47's rollback invariant); neutral
    # defaults until the transcript stage writes them. `transcript_state` is a JobState value.
    transcript_state: str = "none"
    transcript_key: str = ""                  # the content key of the stored transcript
    transcript_version: int = 0               # TRANSCRIBE_VERSION it was made under
    transcript_wake_at: Optional[datetime] = None
    transcript_attempts: int = 0
    transcript_hold: Optional[str] = None
    transcript_hold_version: str = ""
    transcript_stub: str = ""                 # too_large / too_long / no_audio / no_speech / failed
    transcript_seconds: int = 0
    transcript_speakers: int = 0
    transcript_languages: Tuple[str, ...] = ()   # the card's languages (§27.11): the card needs no store read
    transcript_body_version: int = 0          # G18
    transcript_truncated: bool = False        # part of the recording may be missing from the transcript (T-R31)
    # -- §27.9.4 (the mirror queue, D48): set by `drive_mirror_plan.mirror_plan` in `_commit`. `mirror_due` is the queue
    # flag; `mirrored_digest` the digest of what the drive file last showed ("" = never mirrored; a folder is mirrored
    # once it has one); `mirror_retry_at` + `mirror_held_digest` park a record whose action failed (Δ217): it stays off
    # the queue while its digest equals the held one, and a new digest or a ripe `mirror_retry_at` puts it back.
    mirror_due: bool = False
    mirrored_digest: str = ""
    mirror_retry_at: Optional[datetime] = None
    mirror_held_digest: str = ""
    # -- owner notes (DRIVE_FILE_NOTES_RFC §3.3): a denormalized copy of the owner's note bound to this item, rebuilt
    # from the note store by the tick (`drive_note_merge.with_owner_note`); the note store is the record. Plain fields,
    # neutral defaults (Δ47's rollback invariant): a record without a note keeps a byte-identical card (G5). The copy
    # carries the note's id and version and the NOTE_MERGE_VERSION it was merged under (0: no note).
    owner_note_id: str = ""
    owner_note_version: int = 0
    owner_note_text: str = ""
    owner_doc_type: str = ""                  # "" or one drive_labels.DOC_TYPES value
    owner_life_areas: Tuple[str, ...] = ()    # 0-3 drive_labels.LIFE_DOMAINS values, the owner's order
    owner_note_merge_version: int = 0
    # set by the planner's `_build` on a record it creates; `_commit` looks the item's note up by its binding key in the
    # same write and clears it, found or not (§5.1 a). Always False once written.
    note_lookup_due: bool = False
    # -- MULTI_DRIVE_RFC §7.2, §8 (slice 1b): the connection the record belongs to (doc id `record_doc_id(scope, …)`,
    # `user_id` checked on read, I3) and the adapter-built neutral shapes: the item identity (`{id_space}:{…}`: the notes
    # binding, the transcript fallback key, the ledger's source identity), the content identity (the transcript content
    # key and the duplicate collapse; None: no provider hash) and the typed media facts. "" / None / empty facts on a
    # record written before the fields existed (the rebuild writes them, D16).
    connection_id: str = ""
    identity: str = ""
    content_identity: Optional[ContentIdentity] = None
    media: DriveMediaFacts = DriveMediaFacts()

    @property
    def stale(self) -> bool:
        return self.indexed_generation is not None and self.content_version != self.indexed_version


def inaccessible(rec: DriveIndexRecord) -> bool:
    """The bot cannot open this item (§26.2): a 404 on an id-addressed read of an item delta still lists."""
    return rec.inaccessible_since is not None


@dataclass(frozen=True)
class DriveSyncState:
    """One per connection (§3.5, MULTI_DRIVE_RFC §8)."""
    user_id: str
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
    reconnect_notified: bool = False          # the fan-out told the user once (RFC Δ6; R1 too, SECRETS_AT_REST_RFC §6.2)
    # R1 (SECRETS_AT_REST_RFC §6.2, F1): the user disconnected the drive on purpose, so a missing grant is not a loss;
    # set by the Cabinet's disconnect, cleared by a consent; written only by update_sync_flags
    disconnected_by_user_at: Optional[datetime] = None
    counters: Mapping[str, int] = field(default_factory=dict)
    # model work (§24.7, §24.8): an outage defers only model work; usage is counted per calendar month (UTC)
    model_backoff_until: Optional[datetime] = None
    model_failures: int = 0                   # consecutive model outages, for the backoff
    usage_month: str = ""                     # "YYYY-MM" the two counters below belong to
    model_pages_used: int = 0
    triage_images_used: int = 0
    # Search v2 (§25.4.6): one model backoff per agent type (agent type -> until); `model_backoff_until` keeps its v1
    # meaning for the reader and triage
    model_backoff_by_agent: Mapping[str, datetime] = field(default_factory=dict)
    # P5 (§25.8, D30): rebuild progress the tick keeps — profiles `current` (described), and `described_grew_at`: when
    # enrichment last moved (that count grew, a run changed a record) or had no ready job (review rv_sv2_p5 MINOR 2) —
    # and the fan-out's once-only alert flags (written only by update_sync_flags, like stale_alerted)
    described_count: int = 0
    described_grew_at: Optional[datetime] = None
    migration_alerted: bool = False           # awaiting the from-scratch rebuild: alerted once
    stall_alerted: bool = False               # the rebuild stalled for DESCRIBED_STALL_AFTER: alerted once
    fallback_alerted_day: str = ""            # "YYYY-MM-DD" the fallback alert was posted for
    # the fan-out's cap alert (drive_cap_alert): files held by the card cap / the chunk cap, alerted once per incident
    card_cap_alerted: bool = False
    chunk_cap_alerted: bool = False
    purge_keep_transcripts: bool = False      # §27.8 b: the running purge keeps the stored transcripts
    # -- owner notes (DRIVE_FILE_NOTES_RFC §3.4, §5.1 b): the notes step's state, purged with the sync state (each is
    # re-derivable: a purge re-reads every note). The watermark is the (updated_at, note_id) pair of the last note the
    # step processed; `notes_paging`: the last page was full, so the next one starts strictly after that pair (the
    # core's pair cursor); otherwise a new pass starts `NOTE_WATERMARK_SKEW_S` before the earlier of the watermark and
    # `notes_scanned_at` (when the last pass ended), so a late-visible commit is caught and a full skew window is never
    # re-read twice (`drive_note_merge.notes_query_start`). `notes_merge_version` != NOTE_MERGE_VERSION resets the
    # watermark (§5.5). `notes_space` is the embedding space the user's note vectors were last moved to; while it
    # differs from the service's space, a re-embed pass pages through the notes from (`notes_space_at`,
    # `notes_space_id`).
    notes_applied_at: Optional[datetime] = None
    notes_applied_id: str = ""
    notes_paging: bool = False
    notes_scanned_at: Optional[datetime] = None
    notes_merge_version: int = 0
    notes_space: str = ""
    notes_space_at: Optional[datetime] = None
    notes_space_id: str = ""
    # when code with notes support last ran the notes step: a `last_success_at` after it means a tick without that
    # support ran (a rolled-back revision), and the next notes step re-reads every note (plan Default B12)
    notes_tick_at: Optional[datetime] = None
    # owner notes (docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md §3.4, §7.1, §7.4): the archive checks of records with a note that
    # left the index, folded in at every save and worked at the round end; and the daily audit's clock. Both die with
    # the state on a purge, which is right: a purge archives nothing, and the audit is the backstop.
    note_checks: Tuple[NoteCheck, ...] = ()
    notes_audited_at: Optional[datetime] = None
    # the audit pass cursor (§7.4): the (snapshot_at, note_id) of the last note a pass tried; "" = no pass running. A pass
    # cut by its item-call share resumes after it, and `notes_audited_at` is stamped when the pass ends
    notes_audit_after_at: Optional[datetime] = None
    notes_audit_after_id: str = ""
    # -- MULTI_DRIVE_RFC §8, §10 (slice 1b): one sync state per connection (doc id `sync_state_doc_id(scope)` = the
    # connection id; `user_id` checked on read, I3). `drive_identity` is the adapter-built identity of the drive the feed
    # reads (the drive-change check compares it with the connection's).
    connection_id: str = ""
    drive_identity: str = ""

    def cards_in_space(self, embedding_space: str) -> bool:
        """The card stream answers queries embedded in `embedding_space`: no card is left in another space."""
        return self.applied_space == embedding_space

"""
Drive notes — what the owner says about one drive file or folder (docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md §3.1, §3.2,
§4.3, §4.6, §4.8, §15; owner decisions D1-D20). Pure: stdlib, pydantic and other domain modules only; no I/O, no clock,
no logging, no randomness (callers pass `now` and fresh ids).

A note is the triple (text, doc type, life areas), stored bot-side in `{prefix}drive_notes/{user_id}:{note_id}` with
SCD2 versions under `versions/v{n}` (D2). Never on the drive (D1), never a memory fact. A file is identified by the
current provider's item id only (D15): the binding key `i:{provider}:{drive_id}:{item_id}` and the ref key `r:{item_id}`
are the only match keys; the snapshot's content key is data for a future migration, never a lookup.

Public interface:
    NoteState, NoteAction, NoteOrigin, BoundBy, NoteRefusal          (str enums; stored as plain strings)
    DriveNoteRefused(reason, message, **detail)                      a refused call: the tool reports `failed=True`
    DriveNoteConflict                                                a version precondition failed (a concurrent write)
    NoteBinding, NoteSnapshot, NoteWriter                            the note document's maps
    DriveNote, DriveNoteVersion                                      the parent document and one SCD2 version
    NoteContent (the triple), NoteProvenance, NoteTarget, NoteReceipt, EraseRequestReceipt, EraseResult   value objects
    normalize_note_text(text) -> str;  note_word_count(text) -> int;  check_note_text(text) -> str
    resolve_doc_type(value) -> str;  resolve_life_areas(values) -> tuple
    note_content(text, doc_type, life_areas) -> NoteContent
    ONEDRIVE_PROVIDER_KEY (the one provider key of the binding key)
    binding_key(provider_key, drive_id, item_id), ref_key(item_id), note_match_keys(binding)
    BindingKeyParts; parse_binding_key(key) -> Optional[BindingKeyParts]; binding_parts_of(note)
    deterministic_note_id(binding_key) -> str;  note_doc_id(user_id, note_id);  note_ref(note_id)
    parse_note_ref(ref), parse_note_target(ref) -> Optional[NoteTarget]
    snapshot_of(change, item, *, kind, effective_date, now) -> NoteSnapshot
    note_embedding_text(content, name) -> str
    next_version / closed_version / content_of / undo_source / state_after
    note_after_write(...) -> DriveNote;  erase_stub(note) -> DriveNote;  item_ref(note) -> str;  indexer_writer()
    searchability(indexing_enabled, record) -> str
    drive_note_context_entry(receipt) -> str;  is_owner_turn(context) -> bool
    constants: NOTE_WORDS_MAX, NOTE_CHARS_MAX, NOTE_KEYS_MAX, LIFE_AREAS_MAX, ERASE_CODE_TTL, NOTE_WRITES_PER_TURN,
               NOTES_PER_USER_MAX, ARCHIVE_SCAN_MAX, NOTE_KEYS_VERSION, delivery types, tool / skill names, prefixes
"""
from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field

from .drive_index import DriveChange, DriveIndexRecord, RecordState
from .drive_index_params import NOTE_KEYS_MAX   # §5.4: the note's key cap; defined beside the keyer's caps (S2 Core delta B1)
from .drive_card import content_hash_of
from .drive_index_policy import kind_of
from .drive_keys import phrase_keys, token_spans
from .drive_labels import DOC_TYPE_NAMES, DOC_TYPES, LIFE_DOMAIN_NAMES, LIFE_DOMAINS
from .drive_profile import LIFE_DOMAINS_MAX
from .user_drive import DriveItem, make_drive_ref, parse_drive_ref

# -- parameters (§15) ---------------------------------------------------------------------------------------------

NOTE_WORDS_MAX = 80                         # N1: words, counted with the keyer's `token_spans` (script-independent)
NOTE_CHARS_MAX = 600                        # N1: characters (code points) of the normalized text
LIFE_AREAS_MAX = LIFE_DOMAINS_MAX           # 3, the profile's own cap (drive_profile.py)
ERASE_CODE_TTL = timedelta(minutes=10)      # §4.8: an erase code works once, for ten minutes
NOTE_WRITES_PER_TURN = 10                   # §4.2: per execution, then refused (a steered burst is bounded)
NOTES_PER_USER_MAX = 5_000                  # §4.4: lineages per user; a new one beyond is refused
ARCHIVE_SCAN_MAX = 1_000                    # §6.4: archived notes the deleted-files search reads, newest first
NOTE_KEYS_VERSION = 1                       # §5.5: the layout of the stored `keys`

# -- names ----------------------------------------------------------------------------------------------------------

# The provider part of the binding key `i:{provider}:{drive_id}:{item_id}` (D15). ONE source for the main service and the
# indexer, so both build the same key; equal to the OneDrive adapter's credential provider key `ONEDRIVE_PROVIDER`
# (tests/unit/domain/test_drive_note.py pins the equality). A second provider adds its own constant here.
ONEDRIVE_PROVIDER_KEY = "microsoft_onedrive"

DRIVE_FILE_NOTES_SKILL = "drive-file-notes"        # the system skill the handler's gate requires (N7)
DRIVE_NOTE_RECEIPT_DELIVERY = "drive_note_receipt"   # §4.6: DeliveryItem type of a set / clear / restore receipt
DRIVE_NOTE_ERASE_DELIVERY = "drive_note_erase"       # §4.6, §4.8: DeliveryItem type of an erase request
NOTE_REF_PREFIX = "drive_note:"                    # §4.4, §6.4: the note itself, usable with no drive
BINDING_KEY_PREFIX = "i:"
REF_KEY_PREFIX = "r:"
CONTENT_KEY_FAMILY = "quickxor"                    # `content_hash_of`: `quickXorHash:size`
VERSIONS_SUBCOLLECTION = "versions"
ERASE_COMMAND = "$drive-notes erase"
ERASE_ALL_COMMAND = "$drive-notes erase-all"
ERASE_SCOPE_ONE = "one"
ERASE_SCOPE_ALL = "all"
# §4.2 (D5 as amended by D18, N8): the run origins that are an owner turn besides a chat message. An explicit list,
# never a prefix match: a future `voice:` origin is out until it is added on purpose.
OWNER_TURN_RUN_ORIGINS: FrozenSet[str] = frozenset({"voice:ask_alek", "voice:send_to_chat"})

# §4.4 step 7: how soon the note shows in search (the receipt's searchability line)
STATUS_SOON = "soon"                 # a record whose card will carry the note within minutes
STATUS_INDEXING_OFF = "indexing_off"   # the owner's indexing switch is off: stored, not searchable
STATUS_NOT_INDEXED = "not_indexed"   # no record (inside a folder the index keeps as one card, or not seen yet)
STATUS_NO_CARD = "no_card"           # a record without a card it can show (card-capped, refused, unresolved)
STATUS_ARCHIVED = "archived"         # an archived note edited by its drive_note: ref: only a deleted-files search finds it
STATUS_OTHER_DRIVE = "other_drive"   # a note bound to a drive that is not connected: not searchable until it is


# -- enums --------------------------------------------------------------------------------------------------------

class NoteState(str, Enum):
    ACTIVE = "active"        # bound; applies to the card while the item is indexed
    CLEARED = "cleared"      # the owner deleted the note; history kept until he erases it
    ARCHIVED = "archived"    # the item is gone (D9); only the deleted-files search shows it


class NoteAction(str, Enum):
    SET = "set"
    CLEAR = "clear"
    RESTORE = "restore"


class NoteOrigin(str, Enum):
    CHAT = "chat"            # a chat message (a voice memo in chat included)
    VOICE = "voice"          # a call with Lelik, through ask_alek / send_to_chat (D18)
    INDEXER = "indexer"      # lifecycle writes by the tick (never a version)


class BoundBy(str, Enum):
    CHAT = "chat"
    VOICE = "voice"
    RESTORE = "restore"      # re-activated on the same item id (§7.2)


class NoteRefusal(str, Enum):
    """Why a call was refused. The tool turns each into `failed=True` with the reason and what to do."""
    INVALID_REF = "invalid_ref"                  # neither `drive:<id>` nor `drive_note:<id>`
    TOO_LONG = "too_long"                        # detail: words, chars (never cut silently)
    UNKNOWN_DOC_TYPE = "unknown_doc_type"        # detail: value, allowed
    AMBIGUOUS_DOC_TYPE = "ambiguous_doc_type"    # detail: value, candidates
    UNKNOWN_LIFE_AREA = "unknown_life_area"      # detail: value, allowed
    AMBIGUOUS_LIFE_AREA = "ambiguous_life_area"  # detail: value, candidates
    TOO_MANY_LIFE_AREAS = "too_many_life_areas"  # detail: count, max
    SECURITY_REJECTED = "security_rejected"      # blocked or flagged by SecurityPort
    NOT_CONNECTED = "not_connected"              # `set` needs the drive: the item cannot be verified (D17)
    DRIVE_UNAVAILABLE = "drive_unavailable"      # a provider error resolving the item
    NOT_ON_DRIVE = "not_on_drive"                # the provider answered "not found"
    CANNOT_OPEN = "cannot_open"                  # the index marks the item inaccessible (§26.2, D20)
    NO_NOTE = "no_note"                          # undo / erase with no note (a `drive_note:` ref that does not exist)
    AMBIGUOUS_NOTE = "ambiguous_note"            # detail: refs (one item id on several drives)
    HISTORY_MISSING = "history_missing"          # undo found no stored version to copy
    CONFLICT = "conflict"                        # a concurrent write, retried once, still conflicting
    NOTE_LIMIT = "note_limit"                    # NOTES_PER_USER_MAX lineages
    ERASE_UNAVAILABLE = "erase_unavailable"      # no erase-request store wired
    NO_SUCH_ERASE_REQUEST = "no_such_erase_request"   # a wrong, expired, reused or someone else's code
    NOTE_EXISTS = "note_exists"                  # a create over a note that has content: change it instead
    EMPTY_CHANGE = "empty_change"                # an empty argument, or a change that would leave the note empty
    CHANGED_SINCE_READ = "changed_since_read"    # the note changed after the version the caller read


class DriveNoteRefused(Exception):
    """A refused drive-note call. `reason` is a NoteRefusal; `detail` carries the counts, values or refs the tool
    renders ("shorten it to the owner's essentials: 92 words, at most 80")."""

    def __init__(self, reason: NoteRefusal, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.reason = reason
        self.detail: Dict[str, Any] = dict(detail)


class DriveNoteConflict(Exception):
    """The version precondition of a write failed: another write changed the note since it was read."""


# -- documents (§3.1, §3.2) ----------------------------------------------------------------------------------------

_DOC_CONFIG = ConfigDict(use_enum_values=True, validate_default=True, extra="ignore")


class NoteBinding(BaseModel):
    """The item the note is bound to: one item id on one drive of one provider (D15)."""
    model_config = _DOC_CONFIG
    provider_key: str
    drive_id: str
    item_id: str
    bound_at: Optional[datetime] = None
    bound_by: str = BoundBy.CHAT.value


class NoteSnapshot(BaseModel):
    """The item as last seen by the bot. `content_key` is data for a future migration (D15), never a lookup."""
    model_config = _DOC_CONFIG
    name: str = ""
    path: str = ""                       # decoded, relative to the area root
    parent_item_id: str = ""
    is_folder: bool = False
    kind: str = ""
    mime_type: str = ""
    size_bytes: int = 0
    fs_created_at: Optional[datetime] = None
    fs_modified_at: Optional[datetime] = None
    effective_date: Optional[datetime] = None
    content_key: str = ""
    content_key_family: str = ""
    snapshot_at: Optional[datetime] = None


class NoteWriter(BaseModel):
    """Who wrote the parent last: a chat or call turn, or the indexer's lifecycle step."""
    model_config = _DOC_CONFIG
    origin: str = NoteOrigin.CHAT.value
    session_id: str = ""
    turn_id: str = ""


class DriveNote(BaseModel):
    """The parent document `{prefix}drive_notes/{user_id}:{note_id}`: one per noted item (a lineage). Its text and
    classifiers copy `current_version`. An erase stub (§4.8) keeps only the tenant fields, `state = cleared`,
    `erase_pending`, `match_keys` and `updated_at`: `binding`, `snapshot` and `last_writer` are None there."""
    model_config = _DOC_CONFIG
    note_id: str
    user_id: str
    account_id: str = ""
    state: NoteState = NoteState.ACTIVE
    current_version: int = 0
    text: str = ""
    doc_type: str = ""
    life_areas: List[str] = Field(default_factory=list)
    binding: Optional[NoteBinding] = None
    snapshot: Optional[NoteSnapshot] = None
    match_keys: List[str] = Field(default_factory=list)
    keys: List[str] = Field(default_factory=list)
    keys_version: str = ""
    note_vector: Optional[List[float]] = None
    embedding_space: str = ""
    embedding_pending: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None     # always a server timestamp in Firestore (the indexer's watermark)
    archived_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    cleared_at: Optional[datetime] = None
    erase_pending: bool = False
    last_writer: Optional[NoteWriter] = None
    revision: int = 0                         # bumped by the repository on EVERY parent write (owner or lifecycle):
                                              # `write_version`'s precondition, so a lifecycle write in between fails it

    @property
    def doc_id(self) -> str:
        return note_doc_id(self.user_id, self.note_id)

    @property
    def ref(self) -> str:
        """`drive_note:<note_id>`: the note itself, for clear / undo / erase with no drive (§4.4)."""
        return note_ref(self.note_id)

    @property
    def content(self) -> "NoteContent":
        return NoteContent(self.text, self.doc_type, tuple(self.life_areas))


class DriveNoteVersion(BaseModel):
    """`versions/v{n}`: SCD2, as FactEntity. Content fields (`text`, `doc_type`, `life_areas`, `action`, provenance)
    never change; closing a version touches only `valid_to` and `is_current`. Only an erase deletes versions."""
    model_config = _DOC_CONFIG
    version: int
    text: str = ""
    doc_type: str = ""
    life_areas: List[str] = Field(default_factory=list)
    action: NoteAction = NoteAction.SET
    restored_from: Optional[int] = None       # a restore: the version copied (0: before v1, the empty triple)
    valid_from: datetime
    valid_to: Optional[datetime] = None
    is_current: bool = True
    origin: str = NoteOrigin.CHAT.value
    session_id: str = ""
    turn_id: str = ""

    @property
    def content(self) -> "NoteContent":
        return NoteContent(self.text, self.doc_type, tuple(self.life_areas))


# -- value objects ----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class NoteContent:
    """The triple (text, doc type, life areas), already validated (§4.3). All three empty deletes the note."""
    text: str = ""
    doc_type: str = ""
    life_areas: Tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.text and not self.doc_type and not self.life_areas


@dataclass(frozen=True)
class NoteProvenance:
    """Which owner turn wrote a version (§3.2, §4.5 layer 6): `origin` is a NoteOrigin value (`chat` / `voice`)."""
    origin: str = NoteOrigin.CHAT.value
    session_id: str = ""
    turn_id: str = ""


@dataclass(frozen=True)
class NoteTarget:
    """A parsed `file_ref`: exactly one of `note_id` (a `drive_note:` ref) and `item_id` (a `drive:` ref) is set."""
    note_id: str = ""
    item_id: str = ""


@dataclass(frozen=True)
class NoteReceipt:
    """What a set / clear / undo did (§4.6), for the tool result and the receipt. `text`, `doc_type` and `life_areas`
    are the STORED triple (byte for byte from the stored version, never the model's echo). `version` is the note's
    current version after the call (0: there was no note). `noop`: nothing was written (an identical write, or a
    clear with nothing to clear). `redo`: an undo that restored an undone version. `status` is the searchability
    (`STATUS_*`) on a `set`, "" otherwise. `ref` is the bound item's `drive:` ref ("" when unknown)."""
    action: str
    note_id: str
    ref: str
    path: str
    version: int
    text: str = ""
    doc_type: str = ""
    life_areas: Tuple[str, ...] = ()
    previous_text: str = ""
    previous_doc_type: str = ""
    previous_life_areas: Tuple[str, ...] = ()
    status: str = ""
    state: str = ""
    noop: bool = False
    redo: bool = False

    @property
    def note_ref(self) -> str:
        return note_ref(self.note_id) if self.note_id else ""

    def delivery_data(self) -> Dict[str, Any]:
        """The `drive_note_receipt` DeliveryItem's data (§4.6) — the renderer's only input besides its strings."""
        return {"action": self.action, "path": self.path, "text": self.text, "doc_type": self.doc_type,
                "life_areas": list(self.life_areas), "version": self.version, "previous_text": self.previous_text,
                "status": self.status, "ref": self.ref, "note_ref": self.note_ref, "noop": self.noop,
                "redo": self.redo}


@dataclass(frozen=True)
class EraseResult:
    """What `DriveNoteRepository.erase` did: `erased` notes (stubs included), and the ids it could NOT erase (a conflict
    that outlasted its retries, or a store error on that note), in request order; the rest went on regardless."""
    erased: int = 0
    left: Tuple[str, ...] = ()


@dataclass(frozen=True)
class EraseRequestReceipt:
    """An erase request (§4.8): nothing is erased until the owner pastes `command`. `scope` is ERASE_SCOPE_ONE (from
    the tool: `path` and `version_count` of the note) or ERASE_SCOPE_ALL (from `$drive-notes erase-all`)."""
    scope: str
    code: str
    note_count: int
    path: str = ""
    version_count: int = 0

    @property
    def command(self) -> str:
        return f"{ERASE_ALL_COMMAND if self.scope == ERASE_SCOPE_ALL else ERASE_COMMAND} {self.code}"

    def delivery_data(self) -> Dict[str, Any]:
        """The `drive_note_erase` DeliveryItem's data: the path, the version count and the command alone."""
        return {"scope": self.scope, "path": self.path, "version_count": self.version_count,
                "note_count": self.note_count, "command": self.command}


# -- validation and normalization (§4.3) -------------------------------------------------------------------------------

def normalize_note_text(text: str) -> str:
    """NFC; control (Cc) and format (Cf: bidi overrides and isolates, zero-width characters, BOM, soft hyphen)
    characters removed, so the receipt shown "byte for byte" holds nothing invisible; every run of whitespace (newlines
    included) one space; trimmed (one line)."""
    nfc = unicodedata.normalize("NFC", text or "")
    kept = "".join(ch for ch in nfc if ch.isspace() or unicodedata.category(ch) not in ("Cc", "Cf"))
    return " ".join(kept.split())


def note_word_count(text: str) -> int:
    """Words as the keyer counts them (`token_spans`), so the count is script-independent."""
    return len(token_spans(text))


def check_note_text(text: str) -> str:
    """The normalized text, or DriveNoteRefused(TOO_LONG) with both counts: never cut silently."""
    normalized = normalize_note_text(text)
    words, chars = note_word_count(normalized), len(normalized)
    if words > NOTE_WORDS_MAX or chars > NOTE_CHARS_MAX:
        raise DriveNoteRefused(
            NoteRefusal.TOO_LONG,
            f"The note is too long ({words} words, {chars} characters; at most {NOTE_WORDS_MAX} words and "
            f"{NOTE_CHARS_MAX} characters): shorten it to the owner's essentials.",
            words=words, chars=chars, words_max=NOTE_WORDS_MAX, chars_max=NOTE_CHARS_MAX)
    return normalized


def _value_key(value: str) -> str:
    """A label value as typed ("Bank statement", "legal-admin") in the enum's spelling (`bank_statement`)."""
    return "_".join(unicodedata.normalize("NFC", value).casefold().replace("-", " ").split())


def _native_keys(text: str) -> FrozenSet[str]:
    """The keyer's native `e:` phrase forms of a spelling (no skeletons: a translation is not a name)."""
    return frozenset(k for k in phrase_keys(text) if k.startswith("e:"))


def _name_index(names: Mapping[str, Tuple[str, ...]]) -> Dict[str, FrozenSet[str]]:
    return {value: frozenset().union(*(_native_keys(n) for n in spellings)) if spellings else frozenset()
            for value, spellings in names.items()}


_DOC_TYPE_KEYS = _name_index(DOC_TYPE_NAMES)
_LIFE_DOMAIN_KEYS = _name_index(LIFE_DOMAIN_NAMES)


def _resolve_label(value: str, allowed: Sequence[str], index: Mapping[str, FrozenSet[str]],
                   unknown: NoteRefusal, ambiguous: NoteRefusal, what: str) -> str:
    """One label from its enum value or any of its D31 names (matched by the keyer's own forms). An unknown value is
    refused with the list; a name of several values is refused with the candidates (never guessed: D12)."""
    raw = (value or "").strip()
    if not raw:
        return ""
    key = _value_key(raw)
    if key in allowed:
        return key
    wanted = _native_keys(raw)
    candidates = [v for v in allowed if wanted and wanted & index.get(v, frozenset())]
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        raise DriveNoteRefused(ambiguous, f"'{raw}' could be any of {', '.join(candidates)}: say which {what}.",
                               value=raw, candidates=candidates)
    raise DriveNoteRefused(unknown, f"'{raw}' is not a {what}; use one of: {', '.join(allowed)}.",
                           value=raw, allowed=list(allowed))


def resolve_doc_type(value: str) -> str:
    """"" or one DOC_TYPES value, from the value itself or any DOC_TYPE_NAMES name ("договор", "contrato")."""
    return _resolve_label(value, DOC_TYPES, _DOC_TYPE_KEYS, NoteRefusal.UNKNOWN_DOC_TYPE,
                          NoteRefusal.AMBIGUOUS_DOC_TYPE, "document type")


def resolve_life_areas(values: Sequence[str]) -> Tuple[str, ...]:
    """0-LIFE_AREAS_MAX LIFE_DOMAINS values, deduplicated in the owner's order; more is refused (never cut)."""
    resolved: List[str] = []
    for value in values or ():
        area = _resolve_label(value, LIFE_DOMAINS, _LIFE_DOMAIN_KEYS, NoteRefusal.UNKNOWN_LIFE_AREA,
                              NoteRefusal.AMBIGUOUS_LIFE_AREA, "life area")
        if area and area not in resolved:
            resolved.append(area)
    if len(resolved) > LIFE_AREAS_MAX:
        raise DriveNoteRefused(NoteRefusal.TOO_MANY_LIFE_AREAS,
                               f"At most {LIFE_AREAS_MAX} life areas; got {len(resolved)}: {', '.join(resolved)}.",
                               count=len(resolved), max=LIFE_AREAS_MAX)
    return tuple(resolved)


def note_content(text: str, doc_type: str = "", life_areas: Sequence[str] = ()) -> NoteContent:
    """The validated triple; every field refused, never dropped silently. An empty result deletes the note."""
    return NoteContent(check_note_text(text), resolve_doc_type(doc_type), resolve_life_areas(life_areas))


# -- identity and refs (§3.1, §4.4) --------------------------------------------------------------------------------------

def binding_key(provider_key: str, drive_id: str, item_id: str) -> str:
    """`i:{provider}:{drive_id}:{item_id}`: the binding, and the indexer's lookup key (D15: the only identity)."""
    return f"{BINDING_KEY_PREFIX}{provider_key}:{drive_id}:{item_id}"


def ref_key(item_id: str) -> str:
    """`r:{item_id}`: resolves a `drive:<id>` ref with no connection and no drive id (I8)."""
    return f"{REF_KEY_PREFIX}{item_id}"


@dataclass(frozen=True)
class BindingKeyParts:
    """The three parts of a binding key `i:{provider_key}:{drive_id}:{item_id}`."""
    provider_key: str
    drive_id: str
    item_id: str


def parse_binding_key(key: str) -> Optional[BindingKeyParts]:
    """The parts of an `i:` key, None for any other key or a malformed one. The provider key and the drive id hold no
    `:`; the item id is the rest (an item id with a `:` survives the round trip)."""
    if not key or not key.startswith(BINDING_KEY_PREFIX):
        return None
    parts = key[len(BINDING_KEY_PREFIX):].split(":", 2)
    if len(parts) != 3 or not all(parts):
        return None
    return BindingKeyParts(provider_key=parts[0], drive_id=parts[1], item_id=parts[2])


def binding_parts_of(note: DriveNote) -> Optional[BindingKeyParts]:
    """Where a note is bound: its `binding`, or — for an erase stub, which has none — the `i:` key in `match_keys`
    (S2 clears a stub's record copy through it)."""
    if note.binding is not None:
        return BindingKeyParts(note.binding.provider_key, note.binding.drive_id, note.binding.item_id)
    for key in note.match_keys:
        parts = parse_binding_key(key)
        if parts is not None:
            return parts
    return None


def note_match_keys(binding: NoteBinding) -> List[str]:
    """The binding key and the ref key; never a content key (D15), never a provider URL."""
    return [binding_key(binding.provider_key, binding.drive_id, binding.item_id), ref_key(binding.item_id)]


def deterministic_note_id(key: str) -> str:
    """`sha256(i-key)[:32]`: two concurrent first writes for one item collide on one document id (§3.1)."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def note_doc_id(user_id: str, note_id: str) -> str:
    return f"{user_id}:{note_id}"


def note_ref(note_id: str) -> str:
    return f"{NOTE_REF_PREFIX}{note_id}"


def parse_note_ref(ref: str) -> Optional[str]:
    """The note id of a `drive_note:<id>` ref (label punctuation tolerated), None for any other ref."""
    if not ref or NOTE_REF_PREFIX not in ref:
        return None
    tail = ref.split(NOTE_REF_PREFIX, 1)[1]
    note_id = ""
    for ch in tail:
        if ch.isspace() or ch in "\"'[]()<>,;`":
            break
        note_id += ch
    return note_id or None


def parse_note_target(ref: str) -> Optional[NoteTarget]:
    """`drive_note:<id>` → the note; `drive:<id>` → the item; anything else None. The note prefix is tried first."""
    note_id = parse_note_ref(ref)
    if note_id:
        return NoteTarget(note_id=note_id)
    item_id = parse_drive_ref(ref)
    return NoteTarget(item_id=item_id) if item_id else None


# -- the snapshot and the embedding text --------------------------------------------------------------------------------

def snapshot_of(change: DriveChange, item: DriveItem, *, kind: str = "", effective_date: Optional[datetime] = None,
                now: datetime) -> NoteSnapshot:
    """The item as seen by `set` (§4.4 step 2: `get_change` + `get_item`). `kind` and `effective_date` come from the
    index record when there is one; otherwise the kind is the policy's `kind_of` and the date the file's own
    (filesystem-modified, else server-modified). `fs_created_at` is Graph's createdDateTime (the change has no
    filesystem creation date)."""
    content_key = content_hash_of(change) or ""
    return NoteSnapshot(
        name=change.name or item.name, path=item.path, parent_item_id=change.parent_id, is_folder=change.is_folder,
        kind=kind or kind_of(change.name or item.name, change.mime_type, change.is_folder),
        mime_type=change.mime_type, size_bytes=change.size_bytes, fs_created_at=change.created_at,
        fs_modified_at=change.fs_modified_at,
        effective_date=effective_date or change.fs_modified_at or change.modified_at,
        content_key=content_key, content_key_family=CONTENT_KEY_FAMILY if content_key else "", snapshot_at=now)


def _label_name(value: str, names: Mapping[str, Tuple[str, ...]]) -> str:
    spellings = names.get(value) or ()
    return spellings[0] if spellings else value.replace("_", " ")


def note_embedding_text(content: NoteContent, name: str) -> str:
    """What `note_vector` embeds (§3.1): the text, the doc type's and life areas' names (the first D31 name, English),
    and the item's name; one part per line, empty parts left out."""
    parts = [content.text]
    if content.doc_type:
        parts.append(f"type: {_label_name(content.doc_type, DOC_TYPE_NAMES)}")
    if content.life_areas:
        parts.append("life areas: " + ", ".join(_label_name(a, LIFE_DOMAIN_NAMES) for a in content.life_areas))
    parts.append(name)
    return "\n".join(p for p in parts if p)


# -- versions (SCD2, §3.2) and undo (§4.4) --------------------------------------------------------------------------------

def next_version(current_version: int, content: NoteContent, action: NoteAction, provenance: NoteProvenance,
                 now: datetime, *, restored_from: Optional[int] = None,
                 not_before: Optional[datetime] = None) -> DriveNoteVersion:
    """v(current + 1), current, open (`valid_to` None). `valid_from` is the later of `now` and `not_before` (the
    previous version's `valid_from`), so a clock behind the previous writer's never closes a version before it opened."""
    valid_from = max(now, not_before) if not_before is not None else now
    return DriveNoteVersion(
        version=current_version + 1, text=content.text, doc_type=content.doc_type, life_areas=list(content.life_areas),
        action=action, restored_from=restored_from, valid_from=valid_from, valid_to=None, is_current=True,
        origin=provenance.origin, session_id=provenance.session_id, turn_id=provenance.turn_id)


def closed_version(version: DriveNoteVersion, at: datetime) -> DriveNoteVersion:
    """The same version closed at `at`: only `valid_to` and `is_current` change."""
    return version.model_copy(update={"valid_to": at, "is_current": False})


def content_of(version: Optional[DriveNoteVersion]) -> NoteContent:
    return version.content if version is not None else NoteContent()


def undo_source(current_version: int) -> int:
    """The version an undo copies: the one before the current (0: before v1, the empty triple: undo of a first note
    clears it). Undo of an undo copies the undone version back (redo)."""
    return max(current_version - 1, 0)


def state_after(note: Optional[DriveNote], content: NoteContent, action: NoteAction) -> NoteState:
    """The note's state after a write (§4.4): `set` binds to a resolved item → active; `clear` → cleared; a restore of
    the empty triple → cleared; a restore on an archived note keeps it archived; a restore on a cleared note brings
    it back archived when it had been archived (`archived_at` set), else active."""
    if action == NoteAction.SET:
        return NoteState.ACTIVE
    if action == NoteAction.CLEAR or content.is_empty:
        return NoteState.CLEARED
    if note is None:
        return NoteState.ACTIVE
    if note.state == NoteState.ARCHIVED.value:
        return NoteState.ARCHIVED
    if note.state == NoteState.CLEARED.value and note.archived_at is not None:
        return NoteState.ARCHIVED
    return NoteState.ACTIVE


def note_after_write(base: DriveNote, version: DriveNoteVersion, state: NoteState, provenance: NoteProvenance,
                     now: datetime, *, vector: Optional[List[float]], embedding_space: str,
                     embedding_pending: bool, keys: Sequence[str] = (), keys_version: str = "",
                     binding: Optional[NoteBinding] = None, snapshot: Optional[NoteSnapshot] = None) -> DriveNote:
    """The parent document after `version` is written: the text and classifiers copy the version; a cleared note
    empties every field derived from the note (text, classifiers, keys, vector, pending flag) and keeps the snapshot
    and `match_keys` (undo finds it). `binding` / `snapshot` replace the stored ones when given (a `set`), and the
    match keys follow the binding. `updated_at` is left to the repository (a server timestamp)."""
    bound = binding if binding is not None else base.binding
    cleared = state == NoteState.CLEARED
    update: Dict[str, Any] = {
        "state": state.value,
        "current_version": version.version,
        "text": "" if cleared else version.text,
        "doc_type": "" if cleared else version.doc_type,
        "life_areas": [] if cleared else list(version.life_areas),
        "binding": bound,
        "snapshot": snapshot if snapshot is not None else base.snapshot,
        "match_keys": note_match_keys(bound) if bound is not None else list(base.match_keys),
        "keys": [] if cleared else list(keys),
        "keys_version": "" if cleared else keys_version,
        "note_vector": None if cleared else vector,
        "embedding_space": "" if cleared or vector is None else embedding_space,
        "embedding_pending": False if cleared else embedding_pending,
        "created_at": base.created_at or now,
        "cleared_at": now if cleared else None,
        "erase_pending": False,
        "last_writer": NoteWriter(origin=provenance.origin, session_id=provenance.session_id,
                                  turn_id=provenance.turn_id),
    }
    if state == NoteState.ACTIVE and binding is not None:
        update["archived_at"] = None           # resolved on the connected drive: no longer gone
    return base.model_copy(update=update)


def erase_stub(note: DriveNote) -> DriveNote:
    """The contentless stub an erase leaves (§4.8, I9): tenant fields, `state = cleared`, `erase_pending`,
    `match_keys`; text, classifiers, keys, vector, snapshot, binding and last writer gone; no version left."""
    return DriveNote(note_id=note.note_id, user_id=note.user_id, account_id=note.account_id,
                     state=NoteState.CLEARED, current_version=0, match_keys=list(note.match_keys), erase_pending=True,
                     revision=note.revision)   # the repository bumps it with the write


def indexer_writer() -> NoteWriter:
    """`last_writer` of every lifecycle write (archive, re-activation, snapshot refresh, vector repair; §3.1)."""
    return NoteWriter(origin=NoteOrigin.INDEXER.value)


# -- receipts and gates -------------------------------------------------------------------------------------------------

def searchability(indexing_enabled: bool, record: Optional[DriveIndexRecord]) -> str:
    """§4.4 step 7. `soon`: a record whose card will carry the note; `indexing_off`: the switch is off; `not_indexed`:
    no record; `no_card`: a record that shows no card (capped, refused by the embedder, unresolved, or being deleted)."""
    if not indexing_enabled:
        return STATUS_INDEXING_OFF
    if record is None:
        return STATUS_NOT_INDEXED
    if record.card_capped or record.card_refused or record.state in (RecordState.UNRESOLVED, RecordState.DELETING):
        return STATUS_NO_CARD
    return STATUS_SOON


def drive_note_context_entry(receipt: NoteReceipt) -> str:
    """The history context line (§4.6), full text only, never consolidated: `[Drive note: <path> ref=… v<n>]`."""
    ref = receipt.ref or receipt.note_ref
    return f"[Drive note: {receipt.path} ref={ref} v{receipt.version}]"


def is_owner_turn(context: Mapping[str, Any]) -> bool:
    """§4.2 (D5 amended by D18, N8): a chat message (`interactive_delivery`, set only by ConversationHandler) or a
    call with Lelik (`run_origin` exactly one of OWNER_TURN_RUN_ORIGINS, set only by the Alek gateway)."""
    if context.get("interactive_delivery"):
        return True
    origin = context.get("run_origin")
    return isinstance(origin, str) and origin in OWNER_TURN_RUN_ORIGINS


def item_ref(note: DriveNote) -> str:
    """The `drive:` ref of the bound item ("" for a stub)."""
    return make_drive_ref(note.binding.item_id) if note.binding is not None else ""


__all__ = [
    "ARCHIVE_SCAN_MAX", "BINDING_KEY_PREFIX", "BindingKeyParts", "BoundBy",
    "ONEDRIVE_PROVIDER_KEY", "binding_parts_of", "indexer_writer", "parse_binding_key", "CONTENT_KEY_FAMILY",
    "DRIVE_FILE_NOTES_SKILL", "DRIVE_NOTE_ERASE_DELIVERY", "DRIVE_NOTE_RECEIPT_DELIVERY", "DriveNote",
    "DriveNoteConflict", "DriveNoteRefused", "DriveNoteVersion", "ERASE_ALL_COMMAND", "ERASE_CODE_TTL",
    "ERASE_COMMAND", "ERASE_SCOPE_ALL", "ERASE_SCOPE_ONE", "EraseRequestReceipt", "EraseResult", "LIFE_AREAS_MAX", "NOTES_PER_USER_MAX",
    "NOTE_CHARS_MAX", "NOTE_KEYS_MAX", "NOTE_KEYS_VERSION", "NOTE_REF_PREFIX", "NOTE_WORDS_MAX",
    "NOTE_WRITES_PER_TURN", "NoteAction", "NoteBinding", "NoteContent", "NoteOrigin", "NoteProvenance", "NoteReceipt",
    "NoteRefusal", "NoteSnapshot", "NoteState", "NoteTarget", "NoteWriter", "OWNER_TURN_RUN_ORIGINS",
    "REF_KEY_PREFIX", "STATUS_ARCHIVED", "STATUS_INDEXING_OFF", "STATUS_OTHER_DRIVE", "STATUS_NOT_INDEXED", "STATUS_NO_CARD", "STATUS_SOON",
    "VERSIONS_SUBCOLLECTION", "binding_key", "check_note_text", "closed_version", "content_of",
    "deterministic_note_id", "drive_note_context_entry", "erase_stub", "is_owner_turn", "item_ref",
    "next_version", "normalize_note_text", "note_after_write", "note_content", "note_doc_id", "note_embedding_text",
    "note_match_keys", "note_ref", "note_word_count", "parse_note_ref", "parse_note_target", "ref_key",
    "resolve_doc_type", "resolve_life_areas", "searchability", "snapshot_of", "state_after", "undo_source",
]

"""
Drive transcript mirror — the value types of the bot-managed `_Transcripts` folder
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.9, D48, D49). Pure data: no I/O, no clock, no logging.

`MirrorRoot` is the root document (§27.9.2): which folder on a drive is the bot's, plus the mirror's own cursors, clocks
and holds. `MirrorDoc` is one ledger document (§27.9.3): a transcript file, a source folder's counterpart, or an archive
folder. `DeletionNotice` is the durable word that a source item is gone (§27.9.4). States and reasons are plain strings
(they are stored as such); the constants below name the allowed values.

Several public classes in one module, by decision: the value types of one contract (the mirror ledger) are read
together, as `drive_enrichment` does (CLAUDE.md's one-class rule is waived here on purpose).

Public interface:
    MirrorRoot, MirrorDoc, DeletionNotice
    MIRROR_ACTIVE / MIRROR_ARCHIVED / MIRROR_DETACHED     (MirrorDoc.state)
    GONE / UNSTORED / UNKNOWN                              (why a source is gone, DeletionNotice.why)
    KIND_FILE / KIND_FOLDER / KIND_ARCHIVE                 (MirrorDoc.kind)
    ROOT_NAME, README_NAME, ARCHIVE_NAME, LONG_PATHS_NAME  (fixed names, D49)
    doc_key(user_id, kind, source_item_id), archive_folder_key(user_id, drive_id, day, path)
    ledger_key(user_id, kind, mirror_drive_identity, source_identity)    by drive identity (MULTI_DRIVE_RFC §10)
    mirror_root_doc_id(user_id, drive_identity)                           by drive identity (§10)

Multi-drive keys (MULTI_DRIVE_RFC §10, user-scoped by design): a ledger document is keyed by the mirror's drive and the
source item's identity, so two connections that see one source item each keep their own mirror; a root is keyed by
its drive identity, so a reconnect (a new connection of the same drive) finds the same `_Transcripts` folder. An
archive folder's key is `archive_folder_key` called with the drive identity in place of the drive id. `doc_key` is the
single-drive ledger key, kept until its callers move (slice 1b).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

MIRROR_ACTIVE = "active"
MIRROR_ARCHIVED = "archived"
MIRROR_DETACHED = "detached"

GONE = "gone"            # a tombstone: deleted, or moved out of the bot's folder
UNSTORED = "unstored"    # no longer stored but still on the drive
UNKNOWN = "unknown"      # the observation does not say which

KIND_FILE = "file"
KIND_FOLDER = "folder"
KIND_ARCHIVE = "archive"

ROOT_NAME = "_Transcripts"
README_NAME = "_README.md"
ARCHIVE_NAME = "Archive"
LONG_PATHS_NAME = "Long paths"


@dataclass(frozen=True)
class MirrorRoot:
    """`{prefix}drive_mirror_roots/{user_id}:{drive_id}` (§27.9.2). Never deleted by a purge."""
    user_id: str
    drive_id: str
    root_item_id: str
    root_name: str
    readme_item_id: str = ""
    readme_ctag: str = ""
    previous_root_ids: Tuple[str, ...] = ()
    created_at: Optional[datetime] = None
    root_gone_at: Optional[datetime] = None
    seed_version: str = ""
    seed_cursor: str = ""
    scan_cursor: str = ""
    audited_at: Optional[datetime] = None
    audit_folder_key: str = ""
    audit_page_cursor: str = ""
    orphans_reported: bool = False
    quota_hold_until: Optional[datetime] = None
    # the read-me's create intent (§27.9.1, the Δ191/Δ210 pattern): written just before its upload, cleared when the
    # read-me is saved. A re-run adopts a `_README.md` only when it was created at or after this time (minus the
    # clock skew) with the read-me's size, so an owner's `_README.md` is never taken for the bot's
    readme_pending_at: Optional[datetime] = None
    # when step 1 last found the read-me's name held by an item that is not the bot's (§27.9.1): the name is read
    # again only MIRROR_AUDIT_DAYS later (the audit's cadence), not every tick; None when nothing blocks it
    readme_blocked_at: Optional[datetime] = None


@dataclass(frozen=True)
class MirrorDoc:
    """One ledger document (§27.9.3), keyed by `key`. `kind` is file / folder / archive; `state` is active / archived /
    detached. The archive snapshot (`source_path`, `seconds`, `speakers`, `languages`) is refreshed at every render."""
    user_id: str
    drive_id: str
    kind: str
    source_item_id: str
    source_name: str = ""
    source_parent_id: str = ""
    content_key: str = ""
    mirror_item_id: str = ""
    mirror_parent_id: str = ""
    mirror_name: str = ""
    mirror_ctag: str = ""
    rendered_hash: str = ""
    render_version: int = 0
    render_language: str = ""
    state: str = MIRROR_ACTIVE
    source_path: str = ""
    seconds: float = 0.0
    speakers: int = 0
    languages: Tuple[str, ...] = ()
    gone_pending: bool = False
    gone_why: str = ""
    gone_at: Optional[datetime] = None
    mark_ready_at: Optional[datetime] = None
    pending_op: str = ""
    pending_name: str = ""
    pending_parent_id: str = ""
    pending_at: Optional[datetime] = None
    attempts: int = 0
    failed_digest: str = ""      # the record digest the last charged failure happened at ("" for a mark: no record)
    retry_at: Optional[datetime] = None
    failed_at: Optional[datetime] = None
    checked_at: Optional[datetime] = None
    archived_at: Optional[datetime] = None
    archive_day: str = ""
    archive_keys: Tuple[str, ...] = ()
    name_keys: Tuple[str, ...] = ()
    # the file was re-placed in `Long paths` after a path-too-long answer (§27.9.5, Δ204): the placement source it was
    # re-placed for (`placement_source(rec)`), "" when not. While the record's source still equals it, the file's
    # placement is `Long paths`, so a suffixed name there is never moved back every tick; a rename or move of the source
    # re-decides the placement.
    long_path_source: str = ""
    # the byte size of the file a create's intent was about to upload (Δ210): a re-run with no record to render (its
    # source gone) still tells its own file from a foreign one by it; 0 for a folder's intent or none
    pending_size: int = 0
    # when the audit last saw the document's item where the ledger has it (§27.9.9, Δ201): a folder walk spans pages
    # and ticks, so at a folder's last page the documents not seen since the previous cycle ended are the ones to
    # confirm with a read; None until the first audit sees it
    audit_seen_at: Optional[datetime] = None
    # the rest of the render's snapshot (§27.9.3), refreshed with `source_path` at every render: the record's
    # `effective_day` (the header's "recorded on") and `transcript_stub`, so the audit can rewrite an edited ARCHIVED
    # file from the pinned store entry with no record (owner ruling 2026-10-21, Δ241). No link: the recording is gone
    recorded_on: str = ""
    transcript_stub: str = ""

    @property
    def key(self) -> str:
        return doc_key(self.user_id, self.kind, self.source_item_id)


@dataclass(frozen=True)
class DeletionNotice:
    """The durable mark that a source item is gone (§27.9.4): its id, why (GONE / UNSTORED / UNKNOWN), and when."""
    item_id: str
    why: str
    at: datetime


def doc_key(user_id: str, kind: str, source_item_id: str) -> str:
    """`{user_id}:{kind}:{source_item_id}` (§27.9.3)."""
    return f"{user_id}:{kind}:{source_item_id}"


def archive_folder_key(user_id: str, drive_id: str, day: str, path: str) -> str:
    """`{user_id}:archive:{sha256(drive_id + "/" + day + "/" + path)[:32]}` — an archive folder's ledger key (§27.9.3)."""
    digest = hashlib.sha256(f"{drive_id}/{day}/{path}".encode("utf-8")).hexdigest()[:32]
    return f"{user_id}:{KIND_ARCHIVE}:{digest}"


def ledger_key(user_id: str, kind: str, mirror_drive_identity: str, source_identity: str) -> str:
    """`{user_id}:{kind}:{sha256(mirror_drive_identity + "/" + source_identity)[:32]}` (MULTI_DRIVE_RFC §10). Both
    identities are adapter-built and must not be empty (ValueError)."""
    if not mirror_drive_identity or not source_identity:
        raise ValueError("a ledger key needs the mirror's drive identity and the source identity")
    digest = hashlib.sha256(f"{mirror_drive_identity}/{source_identity}".encode("utf-8")).hexdigest()[:32]
    return f"{user_id}:{kind}:{digest}"


def mirror_root_doc_id(user_id: str, drive_identity: str) -> str:
    """`{user_id}:{drive_identity}`: a mirror root's document id (§10). Keyed by the drive, not the connection: the
    folder outlives a connection. ValueError for an empty identity."""
    if not drive_identity:
        raise ValueError("a mirror root key needs the drive identity")
    return f"{user_id}:{drive_identity}"


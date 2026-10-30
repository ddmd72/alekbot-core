"""
Drive transcript mirror — what is due, and what to do about it (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.9.4,
§27.9.5, §27.9.6, §27.9.9; D48, Δ187, Δ188, Δ203, Δ204, Δ207, Δ212, Δ217). Pure: no I/O, no clock (callers pass `now`),
no logging. Every decision here is a function of the values it is given.

How a tick uses it. A record write (`_commit`) runs `mirror_plan(rec)`, which sets the queue flag. The mirror step reads
due records and asks `file_action` / `folder_action` for the NEXT action on the ledger document; an action other than
`stamp_only` leaves the record due, and the step asks again with the updated document until it answers `stamp_only`
(then `stamped(rec)` clears the flag). Marks are decided by `mark_decision`, the root's loss by `root_decision`.

A move is two PATCHes when a name cycle needs a temporary name (`drive_mirror_names.order_moves`). The ledger's
`mirror_name` is what is on the drive: the executor writes it after EACH PATCH, and `source_name` / `source_parent_id`
only after the final one. A tick that stops between the two leaves the temporary `mirror_name`, which differs from the
placement's final name, so `file_action` answers `move` again and the move is finished (tested).

Several public classes in one module, by decision: `Action`, `Decision` and `RootDecision` are the vocabularies of one
contract (the mirror's decisions), read together as `drive_enrichment` does; the one-class rule is waived here.

Public interface:
    MIRROR_PLAN_VERSION
    mirror_eligible(rec), mirror_digest(rec), mirror_plan(rec), stamped(rec), woken(rec), due_again(rec), REDO_DIGEST
    Action, file_action(rec, doc, rendered_hash, placement), folder_action(rec, doc, placement), placement_source(rec)
    Decision, marked(doc, notice), unmarked(doc), mark_held(doc, until), mark_decision(doc, ...)
    RootDecision, root_decision(root, ...)
    digest_changed(doc, digest), retry_due(doc, *, now, digest), charged_doc(doc, now), charged(doc, rec, now),
    reset_charges(doc, digest)
    prune_due(folder_doc, *, source_present, has_child_docs)
    cleared_item(doc), lost_root_reset(doc), audit_due(root, now)
    local_day(at, tz_name)
"""
from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .drive_index import DriveIndexRecord, RecordState
from .drive_index_params import (
    MIRROR_ARCHIVE_GRACE, MIRROR_AUDIT_DAYS, MIRROR_FAILED_RETRY, MIRROR_MAX_ATTEMPTS, MIRROR_ROOT_GRACE,
)
from .drive_enrichment import JobState
from .drive_index_planner import backoff
from .drive_mirror import (
    GONE, KIND_ARCHIVE, KIND_FOLDER, MIRROR_ACTIVE, MIRROR_ARCHIVED, MIRROR_DETACHED, UNKNOWN, UNSTORED, DeletionNotice, MirrorDoc,
    MirrorRoot,
)
from .drive_mirror_names import Placement
from .drive_mirror_render import RENDER_VERSION

MIRROR_PLAN_VERSION = 1


def seed_version() -> str:
    """The root document's `seed_version` once the seed is done (§27.9.4): `"{MIRROR_PLAN_VERSION}.{RENDER_VERSION}"`,
    read at call time. The one formula: the mirror step and the tick both use it."""
    return f"{MIRROR_PLAN_VERSION}.{RENDER_VERSION}"

_SEP = "\x1f"
REDO_DIGEST = "redo"   # `mirrored_digest` of a record whose mirror item must be placed or written again: never a real
                       # digest (those are 32 hex), and not "" (a folder with "" is not eligible, and a record with ""
                       # sends no deletion notice)


class Action(str, Enum):
    CREATE = "create"
    REPLACE = "replace"
    MOVE = "move"
    RESTORE = "restore"
    REATTACH = "reattach"
    STAMP_ONLY = "stamp_only"


class Decision(str, Enum):
    WAIT = "wait"
    CLEAR = "clear"
    ARCHIVE = "archive"
    DETACH = "detach"
    CHECK = "check"


class RootDecision(str, Enum):
    WAIT = "wait"
    CLEAR = "clear"
    CHECK = "check"
    LOST = "lost"


# -- the queue flag -------------------------------------------------------------------------------------------------

def mirror_eligible(rec: DriveIndexRecord) -> bool:
    """A recording whose transcript is `current` (a stub included), or a folder that already has a mirror counterpart
    (§27.9.4). A transcript that is not current is never due: its last file stays until the new one is current."""
    if rec.is_folder:
        return rec.mirrored_digest != "" and rec.state not in (RecordState.DELETING, RecordState.UNRESOLVED)
    return rec.transcript_state == JobState.CURRENT.value


def mirror_digest(rec: DriveIndexRecord) -> str:
    """Hash of what the drive file shows (§27.9.4, Δ207). A recording: name, parent, `web_url`, the transcript's key,
    version, stub, truncated flag, seconds, speakers, languages and body version, and the two versions. A folder: name
    and parent. The UI language is not in it (Δ120)."""
    if rec.is_folder:
        parts: Tuple[str, ...] = ("folder", rec.name, rec.parent_id)
    else:
        parts = ("file", rec.name, rec.parent_id, rec.web_url, rec.transcript_key, str(rec.transcript_version),
                 rec.transcript_stub, str(int(rec.transcript_truncated)), str(rec.transcript_seconds),
                 str(rec.transcript_speakers), ",".join(rec.transcript_languages), str(rec.transcript_body_version),
                 str(MIRROR_PLAN_VERSION), str(RENDER_VERSION))
    return hashlib.sha256(_SEP.join(parts).encode("utf-8")).hexdigest()[:32]


def mirror_plan(rec: DriveIndexRecord) -> DriveIndexRecord:
    """Sets `mirror_due`, and the hold fields when a new digest ends a hold; touches nothing else (§27.9.4).
    Due when eligible and the digest differs from `mirrored_digest` and is not the held one (Δ217); a new digest
    clears the hold at once."""
    if not mirror_eligible(rec):
        return replace(rec, mirror_due=False)
    digest = mirror_digest(rec)
    if digest == rec.mirrored_digest:
        return replace(rec, mirror_due=False, mirror_retry_at=None, mirror_held_digest="")
    if rec.mirror_held_digest == digest:
        return replace(rec, mirror_due=False)
    return replace(rec, mirror_due=True, mirror_retry_at=None, mirror_held_digest="")


def stamped(rec: DriveIndexRecord) -> DriveIndexRecord:
    """After a successful action: `mirrored_digest` = the digest now, no hold, and `mirror_plan` clears the flag. A
    source folder's record is stamped the moment its mirror folder document is created (Δ113's one move)."""
    return mirror_plan(replace(rec, mirrored_digest=mirror_digest(rec), mirror_retry_at=None, mirror_held_digest=""))


def due_again(rec: DriveIndexRecord) -> DriveIndexRecord:
    """The record's mirror work redone (§27.9.9: the audit's repairs, a confirmed root loss): `mirrored_digest` becomes
    REDO_DIGEST, so `mirror_plan` makes an eligible record due (a folder that has a counterpart included) and a later
    delete of it still sends the mirror its notice."""
    return mirror_plan(replace(rec, mirrored_digest=REDO_DIGEST))


def woken(rec: DriveIndexRecord) -> DriveIndexRecord:
    """Clears a ripe hold (the wake-up query found `mirror_retry_at <= now`); `mirror_plan` makes the record due again."""
    return mirror_plan(replace(rec, mirror_retry_at=None, mirror_held_digest=""))


# -- the next action ------------------------------------------------------------------------------------------------

def _wanted_name(placement: Placement) -> str:
    return placement.parts[-1]


def _misplaced(rec: DriveIndexRecord, doc: MirrorDoc, placement: Placement) -> bool:
    """The drive item is not where the source says: source name or parent changed, or the drive name is not the final one
    (a temporary name left by a stopped tick counts)."""
    return (doc.source_name != rec.name or doc.source_parent_id != rec.parent_id
            or doc.mirror_name != _wanted_name(placement))


def placement_source(rec: DriveIndexRecord) -> str:
    """What a `Long paths` re-placement was decided for (`MirrorDoc.long_path_source`): the source's name and parent.
    A rename or move of the source changes it, and the placement is decided again."""
    return hashlib.sha256(_SEP.join(("place", rec.name, rec.parent_id)).encode("utf-8")).hexdigest()[:16]


def file_action(rec: DriveIndexRecord, doc: Optional[MirrorDoc], rendered_hash: str, placement: Placement) -> Action:
    """The next action for a due recording (§27.9.5 step 4). `placement` is where the file belongs now, its last part
    the final (collision-assigned) name. No document: create. Archived (the record is current again): restore.
    Detached (the record is back): reattach. Active: move when the source name or parent changed or the drive name is
    not the final one (a temporary name left by a stopped tick, see the module note); replace only when the rendered
    hash differs; otherwise only the digest is stamped. A document with no `mirror_item_id` holds only a create's intent
    (written before the drive call, Δ210): it is a create, whatever its state."""
    if doc is None or not doc.mirror_item_id:
        return Action.CREATE
    if doc.state == MIRROR_ARCHIVED:
        return Action.RESTORE
    if doc.state == MIRROR_DETACHED:
        return Action.REATTACH
    if _misplaced(rec, doc, placement):
        return Action.MOVE
    if doc.rendered_hash != rendered_hash:
        return Action.REPLACE
    return Action.STAMP_ONLY


def folder_action(rec: DriveIndexRecord, doc: Optional[MirrorDoc], placement: Placement) -> Action:
    """A source folder's counterpart: create, move (renamed, re-parented, or not at its final name), or stamp only."""
    if doc is None or not doc.mirror_item_id:
        return Action.CREATE
    if _misplaced(rec, doc, placement):
        return Action.MOVE
    return Action.STAMP_ONLY


# -- marks ----------------------------------------------------------------------------------------------------------

_WHY_RANK = {UNKNOWN: 0, UNSTORED: 1, GONE: 2}


def marked(doc: MirrorDoc, notice: DeletionNotice) -> MirrorDoc:
    """The durable word that the source is gone (§27.9.4): ready one grace after it was seen. On a document already
    marked the earliest `gone_at` stays, `mark_ready_at` only moves later, and `why` only upgrades
    (unknown < unstored < gone), so a repeated notice never postpones or weakens a mark."""
    ready = notice.at + MIRROR_ARCHIVE_GRACE
    if not doc.gone_pending:
        return replace(doc, gone_pending=True, gone_why=notice.why, gone_at=notice.at, mark_ready_at=ready)
    gone_at = min(doc.gone_at, notice.at) if doc.gone_at is not None else notice.at
    why = notice.why if _WHY_RANK.get(notice.why, 0) > _WHY_RANK.get(doc.gone_why, 0) else doc.gone_why
    later = max(doc.mark_ready_at, ready) if doc.mark_ready_at is not None else ready
    return replace(doc, gone_why=why, gone_at=gone_at, mark_ready_at=later)


def unmarked(doc: MirrorDoc) -> MirrorDoc:
    return replace(doc, gone_pending=False, gone_why="", gone_at=None, mark_ready_at=None)


def mark_held(doc: MirrorDoc, until: datetime) -> MirrorDoc:
    """Moves a mark's `mark_ready_at` later (a failure, or an ancestor still re-listing); never earlier."""
    if doc.mark_ready_at is not None and doc.mark_ready_at >= until:
        return doc
    return replace(doc, mark_ready_at=until)


def mark_decision(doc: MirrorDoc, *, record_present: bool, ancestor_reobserving: bool, now: datetime,
                  change_found: Optional[bool] = None) -> Decision:
    """What to do with a mark (§27.9.5 step 3). The record back (a restore, a move back in) clears it at any time. An
    unripe mark (inside the grace, or held), or an ancestor folder still re-listing (Δ212), waits. Then by why: `gone`
    archives, `unstored` detaches, `unknown` needs one read of the source (`change_found` None: CHECK; not found:
    archive, Δ211; found with no record: detach). An already archived document has nothing to do."""
    if not doc.gone_pending or doc.state == MIRROR_ARCHIVED or record_present:
        return Decision.CLEAR
    ready = doc.mark_ready_at
    if ready is None and doc.gone_at is not None:
        ready = doc.gone_at + MIRROR_ARCHIVE_GRACE
    if ready is None or now < ready or ancestor_reobserving:
        return Decision.WAIT
    if doc.gone_why == GONE:
        return Decision.ARCHIVE
    if doc.gone_why == UNSTORED:
        return Decision.DETACH
    if doc.gone_why == UNKNOWN or doc.gone_why == "":
        if change_found is None:
            return Decision.CHECK
        return Decision.DETACH if change_found else Decision.ARCHIVE
    return Decision.CHECK


def root_decision(root: MirrorRoot, *, record_present: bool, round_complete: bool, now: datetime,
                  change_found: Optional[bool] = None) -> RootDecision:
    """The root's loss is graced and reversible until confirmed (Δ203): the record back clears the mark; the mirror acts
    only after a complete round and MIRROR_ROOT_GRACE; then one read of the root decides (found: clear; not found:
    lost)."""
    if root.root_gone_at is None or record_present:
        return RootDecision.CLEAR
    if not round_complete or now < root.root_gone_at + MIRROR_ROOT_GRACE:
        return RootDecision.WAIT
    if change_found is None:
        return RootDecision.CHECK
    return RootDecision.CLEAR if change_found else RootDecision.LOST


# -- failures -------------------------------------------------------------------------------------------------------

def digest_changed(doc: MirrorDoc, digest: str) -> bool:
    """The record's digest is not the one the document last failed at (`failed_digest`): new content, not a wake-up."""
    return doc.failed_digest != "" and digest != doc.failed_digest


def retry_due(doc: MirrorDoc, *, now: datetime, digest: str = "") -> bool:
    """May the step try this document now (Δ204)? A new digest: yes. Parked (`failed_at`): after MIRROR_FAILED_RETRY.
    Backing off: after `retry_at`. Contract: on the DUE path the queue is the gate (a record in `mirror_queue`, or just
    woken, is always tried); this function gates MARKS, which have no record (`digest` "" then)."""
    if digest and digest_changed(doc, digest):
        return True
    if doc.failed_at is not None:
        return now >= doc.failed_at + MIRROR_FAILED_RETRY
    if doc.retry_at is not None:
        return now >= doc.retry_at
    return True


def charged_doc(doc: MirrorDoc, now: datetime) -> MirrorDoc:
    """One failed action on a document with no record (a mark): `attempts` + 1 and `retry_at = now + backoff(attempts)`;
    at MIRROR_MAX_ATTEMPTS `failed_at = now` (parked)."""
    attempts = doc.attempts + 1
    parked = attempts >= MIRROR_MAX_ATTEMPTS
    return replace(doc, attempts=attempts, retry_at=now + backoff(attempts), failed_at=now if parked else doc.failed_at)


def charged(doc: MirrorDoc, rec: DriveIndexRecord, now: datetime) -> Tuple[MirrorDoc, DriveIndexRecord]:
    """One failed action on due work (Δ204, Δ217): `charged_doc`, plus `failed_digest` on the document. The record leaves
    the queue: `mirror_due` False, `mirror_retry_at` the same time (the park's end when parked), `mirror_held_digest` its
    digest (the planner's backoff, as `with_retry` uses it)."""
    digest = mirror_digest(rec)
    new_doc = replace(charged_doc(doc, now), failed_digest=digest)
    held_until = now + MIRROR_FAILED_RETRY if new_doc.failed_at is not None else new_doc.retry_at
    return new_doc, replace(rec, mirror_due=False, mirror_retry_at=held_until, mirror_held_digest=digest)


def reset_charges(doc: MirrorDoc, digest: Optional[str] = None) -> MirrorDoc:
    """A fresh retry budget: no attempts, no backoff, not parked. With `digest`, only when it is new against the one the
    document failed at (`digest_changed`); without, always (a success)."""
    if digest is not None and not digest_changed(doc, digest):
        return doc
    return replace(doc, attempts=0, retry_at=None, failed_at=None, failed_digest="")


# -- pruning --------------------------------------------------------------------------------------------------------

def prune_due(folder_doc: MirrorDoc, *, source_present: bool, has_child_docs: bool) -> bool:
    """An active mirror FOLDER whose source folder is gone and which holds no child document is to be listed once and,
    when empty, deleted (§27.9.5 step 5, Δ197). The archive folder and its day folders are never pruned."""
    return (folder_doc.kind == KIND_FOLDER and folder_doc.state == MIRROR_ACTIVE
            and not source_present and not has_child_docs)


# -- the drive item gone, the root lost, the audit -------------------------------------------------------------------

def cleared_item(doc: MirrorDoc) -> MirrorDoc:
    """The document with no drive item and no create intent (§27.9.5, §27.9.9): its file or folder is not on the drive
    (or no longer under the root), so it is created again when its record is due. Its state, its mark and its archive
    snapshot stay (a detached document keeps its pin)."""
    return replace(doc, mirror_item_id="", mirror_parent_id="", mirror_name="", mirror_ctag="", rendered_hash="",
                   pending_op="", pending_name="", pending_parent_id="", pending_at=None, pending_size=0,
                   audit_seen_at=None)


def lost_root_reset(doc: MirrorDoc) -> Optional[MirrorDoc]:
    """A document after a confirmed root loss (§27.9.9, Δ200): None for an archived file or an archive folder (the
    owner deleted or moved away the archive with the root; the document goes and its pin with it); every other
    document is `cleared_item` with a fresh retry budget, so it is created again under the new root and never points
    at an item of the previous one. A detached document keeps its state, so its store entry stays pinned."""
    if doc.kind == KIND_ARCHIVE or doc.state == MIRROR_ARCHIVED:
        return None
    return reset_charges(cleared_item(doc))


def audit_due(root: MirrorRoot, now: datetime) -> bool:
    """The audit runs at most every MIRROR_AUDIT_DAYS (§27.9.9): due once that long has passed since the last cycle
    ended (`audited_at`), or since the root was created when no cycle has run yet. A cycle under way stays due, since
    `audited_at` is written only at its end."""
    base = root.audited_at or root.created_at
    return base is None or now >= base + timedelta(days=MIRROR_AUDIT_DAYS)


# -- the archive day ------------------------------------------------------------------------------------------------

def local_day(at: datetime, tz_name: str) -> str:
    """`YYYY-MM-DD` of `at` in the user's time zone (`UserBotConfig.timezone`), else UTC (§27.9.6): the archive folder's
    name. A naive `at` is UTC. The archive day is computed by Task 21A from `doc.gone_at`, once, when the file is archived, and stored in
    `archive_day`."""
    aware = at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)
    try:
        zone: Optional[ZoneInfo] = ZoneInfo(tz_name) if tz_name else None
    except (ZoneInfoNotFoundError, ValueError):   # unknown or malformed zone name: UTC
        zone = None
    local = aware.astimezone(zone) if zone is not None else aware.astimezone(timezone.utc)
    return local.date().isoformat()


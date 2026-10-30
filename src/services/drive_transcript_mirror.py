"""
DriveTranscriptMirror — the reconciliation step that writes the bot-managed `_Transcripts` folder on the user's drive
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.9.5-§27.9.7; D48, D49; Δ189-Δ217). Runs last in the tick, for one user,
through a `MirrorHost` the tick lends it (Task 21C): the budgets, the fence, the clock, the strings and the record writes.

Order of a run (§27.9.5):
    0. the root-loss check (`_root_step`, §27.9.9, Δ203): while the root-loss mark is set nothing else runs (Δ217); after
       a complete round and MIRROR_ROOT_GRACE one `get_change(root)` decides: found → the mark is cleared and the run
       goes on; not found → the loss is confirmed: the old id joins `previous_root_ids` (which also marks the ledger
       reset as under way), archived documents and archive folders are deleted, every other document loses its drive
       item (`lost_root_reset`, paged across ticks), then step 1 creates a new root;
    -  while `quota_hold_until` is in the future nothing else runs either (a full drive, Δ204);
    1. root setup: `_Transcripts` (then `(Alek)`, `(Alek 2)` .. `(Alek 5)`) created under the App Folder root's item id,
       the root document saved the moment Graph answers, then `_README.md` (`fail`; a re-run reads the name first);
    2. one seed page while `seed_version` differs (records written before T2 or before a version bump become due);
    3. marks older than the grace (`ready_marks`), files before folders: clear, archive, detach, or one `get_change`;
    4. wake-ups, then due records newest first: create, replace, move, restore, reattach, stamp;
    5. prune: a gone source folder's empty mirror folder, after one `list_children_changes(limit=1)`;
    6. the rotating scan (§27.9.9), only while the epoch is idle after a complete round: MIRROR_SCAN_PAGE file
       documents from `scan_cursor`; an active one whose record is missing and which has no mark gets `unknown` (never
       filtered on the record's digest); a detached one not checked for MIRROR_AUDIT_DAYS gets one `get_change` of its
       source (not found → a `gone` mark, so the mark step archives it);
    7. the audit (§27.9.9, Δ201), at most every MIRROR_AUDIT_DAYS: the root, then every folder and archive-folder
       document in key order, then the App Folder root, one listing page per read from the mirror's own read budget
       (`audit_folder_key`, `audit_page_cursor` = Graph's next link). Listed items are compared with the folder's
       documents; at a folder's last page the documents not seen this cycle (`audit_seen_at`) are read once each.
       Active tree: a missing file is created again from the store (its record made due), a changed `cTag` rewritten
       (made due with no rendered hash, also when it was moved as well, Δ242), a moved or renamed item moved back. An
       archived or a detached file is the bot's too (owner ruling 2026-10-21, Δ241, Δ243): an edit is rewritten by the
       audit itself from the pinned store entry (`render_archived`), a rename or a move inside the mirror moved back.
       Archive folders: an edit or a move inside the mirror is followed, never written. A missing file or one moved out of the mirror ends
       the document (archived: deleted, its pin released — a copy elsewhere is the owner's; detached: no item, its pin
       kept). A folder answering 404
       treats every document beneath it the same way. `_README.md` is rewritten when changed and written again when
       missing; `Long paths` is created on demand only. Foreign items are counted and left alone. At the App Folder
       root a folder named like the root that is no known root, or a previous root present again, is reported once
       (`orphans_reported`), never adopted or written.

Rules every write keeps:
- **A create never overwrites** (Δ191, Δ210): it writes its intent (`pending_*`) first and uses `fail`; a re-run with an
  intent reads the name (`child_by_name`): absent → create; ours (created at or after `pending_at - MIRROR_CLOCK_SKEW`,
  a file of the rendered size) → adopted, then rewritten by id; anything else → a foreign item, whose name joins the
  folder's taken names. `replace` is never sent at a name. A 409 on a create or a move adds the name to the taken
  names and the names are assigned again.
- **Names** are assigned over the whole target folder each time (`assign_names`), the siblings' wanted names read from
  their records when those are mirror-eligible in the same folder, and the implied renames executed first
  (`order_moves`), each PATCH recorded on its document at once (a tick stopping between a temporary and a final move
  leaves the temporary name, which is finished later). A name freed by a move, an archive or a prune makes the step
  assign that folder's names again and rename the suffixed documents whose collision is gone (within the budget).
  Archive folders are frozen: only the newcomer is assigned, against every name already there.
- **Errors never stall the index** (Δ204): DriveThrottled, DriveNotConnectedError (DriveReauthRequired included),
  DriveIndexStoreUnavailable and LeaseLost leave the step; DriveStorageFull holds the mirror for MIRROR_QUOTA_HOLD and
  ends the step without charging a document; DrivePathTooLong re-places a file in `Long paths` once (persisted as
  `long_path_source`), then is charged; ValueError, OSError, TimeoutError, DriveAccessDenied (and a name conflict that
  outlives the retries) charge the document; anything else is logged at ERROR, counted and skipped.
- **Budgets** (§27.9.5): every Graph write takes one of `ops_left`, every Graph read one of `reads_left` (the audit's
  and the scan's too: never the index's item calls), every due record that ends in a stamp with no write one of
  `stamps_left`; a spent budget or deadline ends the step. A walk that stops keeps its cursor and resumes.
- **Never another's item** (D42, Δ116, Δ117): the bot moves, rewrites or deletes only items its ledger holds (created
  by it, or adopted by its own recorded intent); a foreign item is counted and left alone, an orphan root reported, a
  previous root never written.
- **Logs** carry counts, short ids, document keys' kinds and error types only: never a name or path of the owner's
  files, a URL or transcript text.

The archive day is `local_day(doc.gone_at, host.tz_name)` (when the indexer saw the source go, in the user's zone),
never the day of the run. `recorded_on` in a file header is the record's `effective_day` (a stable record field).
`source_path` on a document (and in the header) is the source's FOLDER path relative to the App Folder ("" at the top);
the file's name is the heading.

Public interface:
    MirrorHost                                  the tick's side (a Protocol; Task 21C's `_TickHost` implements it)
    DriveTranscriptMirror(repo, mirror, drive)
        run(handle, host) -> Dict[str, int]     steps 0-7 for one connection's mirror; the counters of the run

Keys (MULTI_DRIVE_RFC §10): the root is the user's root of the connection's drive identity; a file's or folder's ledger
document is `ledger_key(user, kind, drive identity, source item identity)`, so two connections that see one source item
keep their own mirror, and every drive-scoped read filters the drive identity.
"""
from __future__ import annotations

import functools
import hashlib
import re
import traceback
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol, Sequence, Set, Tuple, Type

from src.domain import drive_mirror_plan as plan
from src.domain.drive_handle import DriveHandle
from src.domain.drive_index import (
    DriveChange, DriveIndexRecord, DriveIndexStoreUnavailable, LeaseLost, RecordState, Scope,
)
from src.domain.drive_index_params import (
    MIRROR_ARCHIVE_GRACE, MIRROR_AUDIT_DAYS, MIRROR_CLOCK_SKEW, MIRROR_QUOTA_HOLD, MIRROR_SCAN_PAGE, MIRROR_SEED_PAGE,
)
from src.domain.drive_keys import name_keys_v2
from src.domain.drive_mirror import (
    ARCHIVE_NAME, GONE, KIND_ARCHIVE, KIND_FILE, KIND_FOLDER, LONG_PATHS_NAME, MIRROR_ACTIVE, MIRROR_ARCHIVED,
    MIRROR_DETACHED, README_NAME, UNKNOWN, DeletionNotice, MirrorDoc, MirrorRoot, archive_folder_key, ledger_key,
)
from src.domain.drive_mirror_names import (
    LEVEL_DAY, LEVEL_OTHER, LEVEL_ROOT, Move, Placement, Sibling, archive_place, assign_names, fits, fold,
    is_root_name, mirror_file_name, order_moves, reserved_names, root_name_candidates,
)
from src.domain.drive_mirror_plan import (
    REDO_DIGEST, Action, Decision, RootDecision, audit_due, due_again, charged, charged_doc, cleared_item, file_action, local_day,
    lost_root_reset, mark_decision, mark_held, marked, mirror_digest, mirror_eligible, mirror_plan, placement_source,
    reset_charges, retry_due, root_decision, seed_version, stamped, unmarked, woken,
)
from src.domain.drive_mirror_render import (
    MirrorStrings, archive_word_keys, readme_bytes, render_archived, render_file, rendered_hash,
)
from src.domain.drive_transcript import StoredTranscript
from src.domain.user_drive import (
    DriveAccessDenied, DriveItemNotFoundError, DriveNameConflictError, DriveNotConnectedError, DrivePathTooLong,
    DriveStorageFull, DriveThrottled,
)
from src.ports.drive_index_repository import DriveIndexRepository
from src.ports.drive_mirror_repository import DriveMirrorRepository
from src.ports.user_drive_port import UserDrivePort
from src.utils.logger import logger

MD_TYPE = "text/markdown"
QUEUE_PAGE = 50                  # due records read per page of `mirror_queue`
WAKEUP_PAGE = 50                 # ripe holds woken per run
MARKS_PAGE = 40                  # ready marks read per run (each is at most one move)
CHILDREN_MAX = 2_000             # ledger documents of one mirror folder read for a name assignment
ANCESTOR_HOLD = timedelta(minutes=10)     # a mark waiting for an ancestor's re-listing, or for the files beneath it
FOREIGN_FOLDER_HOLD = timedelta(days=MIRROR_AUDIT_DAYS)   # a gone folder kept for a foreign item is listed again then
MAX_CHAIN_DEPTH = 64             # folders above a source item
MAX_ACTIONS = 8                  # actions on one due record in one run (create, then replace, ...): a guard, not a rule
NAME_RETRIES = 4                 # name assignments of one placement after 409s
AUDIT_PAGE = 200                 # children of one folder per audit listing (one Graph read)
RESET_PAGE = 200                 # ledger documents per page of the reset after a confirmed root loss
AUDIT_AREA = "@app-folder"       # `audit_folder_key` of the walk's last stage, the App Folder root (no doc key: no ":")
AUDIT_CONFIRM = "@confirm"       # `audit_page_cursor` once a folder's listing is done: its unseen documents are next
AUDIT_GONE = "@gone"             # `audit_page_cursor` once a folder's listing answered 404: its confirming read is next
_RESET_FILES = "f"               # `scan_cursor` while a confirmed loss resets the ledger: "f|<after key>" (files) ...
_RESET_FOLDERS = "d"             # ... then "d|<after key>" (folder and archive documents)

_SYSTEMIC = (DriveThrottled, DriveNotConnectedError, DriveIndexStoreUnavailable, LeaseLost)
_CHARGED = (ValueError, OSError, TimeoutError, DriveAccessDenied, DriveNameConflictError, DrivePathTooLong)
_SUFFIX = re.compile(r"~[0-9a-f]{8}$")
_TMP = re.compile(r"~tmp[0-9a-f]{8}$")


class MirrorHost(Protocol):
    """What the tick lends the step, for one user. Every write the step makes goes through the tick's rules."""

    drive_identity: str    # the mirror's drive: the connection's drive identity (MULTI_DRIVE_RFC §10); "" → skips
    tz_name: str           # `UserBotConfig.timezone` ("" → UTC): the archive day of `gone_at`
    language: str          # the UI language the strings are in, stored as `render_language`
    round_complete: bool   # the change feed's round is complete (marks wait for it)
    epoch_idle: bool       # no enumeration or sweep is running (the scan's condition, Task 21B)
    stop_exceptions: Tuple[Type[BaseException], ...]   # the host's own stop signals (e.g. indexing turned off mid-tick)

    def fence(self) -> None:
        """Raise `LeaseLost` once the tick's lease is about to end. Called before every write, store or Graph."""

    async def commit(self, records: Sequence[DriveIndexRecord]) -> None:
        """Write records the way every tick write goes (`_commit`: fenced, `mirror_plan` applied)."""

    def ops_left(self) -> int:
        """Graph writes left this tick (MIRROR_OPS_PER_TICK)."""

    def spend_op(self) -> None: ...

    def reads_left(self) -> int:
        """Graph reads left this tick (MIRROR_READS_PER_TICK, the mirror's own bucket)."""

    def spend_read(self) -> None: ...

    def stamps_left(self) -> int:
        """Due records left this tick that may end in a stamp with no Graph write (MIRROR_STAMPS_PER_TICK)."""

    def spend_stamp(self) -> None: ...

    def time_left(self) -> float:
        """Seconds left in the step's window (the host's own bound for the mirror step, never later than the tick's
        deadline); 0 or less ends the step."""

    def now(self) -> datetime: ...

    def day(self) -> str:
        """The user's local date, `YYYY-MM-DD` (the tick's day; not the archive day, which is `gone_at`'s)."""

    def strings(self) -> MirrorStrings:
        """The localized strings of the file header, the stubs and the read-me, in `language`."""

    def root_mark_changed(self, root_item_id: str, gone: bool) -> None:
        """The step changed the root-loss state of that root during the tick; the tick keeps its own list of marked
        current roots (`_Mirror.gone_ids`, Task 21C) in step: `gone=True` ADDS the id (the step set the mark after a
        not-found answer), `gone=False` DISCARDS it (the mark was cleared, or the loss was confirmed). On a confirmed
        loss it is called with False while `root_gone_at` is still set on the root document (the old id now sits in
        `previous_root_ids` and is no longer a current root): the tick must take this call as the truth and never
        re-sync its list from that document's `root_gone_at` during the tick."""


class DriveTranscriptMirror:
    """The mirror step (§27.9.5). Ports only (REQ-ARCH-22): never `UserDriveService`, whose guard refuses the mirror."""

    def __init__(self, repo: DriveIndexRepository, mirror: DriveMirrorRepository, drive: UserDrivePort) -> None:
        self._repo = repo
        self._mirror = mirror
        self._drive = drive
        # (connection id, item id) -> holds of a record whose document cannot be read or written (`_hold_record`)
        self._held_attempts: Dict[Tuple[str, str], int] = {}

    async def run(self, handle: DriveHandle, host: MirrorHost) -> Dict[str, int]:
        """Steps 0-5 for one user, within the host's budgets. Raises only the systemic classes; every other failure is
        contained in its action (charged), and anything outside an action (a page read, a decode) is logged at ERROR and
        counted (`mirror_errors`), so the tick's counter refresh after the step always runs. Returns the counters."""
        counters: Counter = Counter()
        user_id = handle.user_id
        if not host.drive_identity:
            counters["mirror_no_drive_id"] += 1
            return dict(counters)
        ctx = _Run(user_id, handle, host, counters)
        try:
            await self._steps(ctx)
        except _StopStep:
            logger.debug(f"[DriveMirror] user={user_id[:8]} step ended early (a budget, the deadline or a hold)")
        except _SYSTEMIC + ctx.stops:
            raise
        except Exception as exc:              # a page read or a decode outside every action: never out of the step
            _log_unexpected(user_id, "step", exc, "the step ended")
            counters["mirror_errors"] += 1
        summary = {k: v for k, v in sorted(counters.items()) if v}
        if summary:
            logger.info(f"[DriveMirror] user={user_id[:8]} {summary}")
        return dict(counters)

    # =================================================================================================================
    # implementation
    # =================================================================================================================

    async def _steps(self, ctx: "_Run") -> None:
        root = await self._mirror.get_root(ctx.user_id, ctx.drive_identity)
        if root is not None and root.drive_identity != ctx.drive_identity:
            logger.error(f"[DriveMirror] user={ctx.user_id[:8]} root document of another drive returned: skipped")
            return
        if not await self._root_step(ctx, root):
            return
        root = ctx.root
        if root is not None and root.quota_hold_until is not None and ctx.now < root.quota_hold_until:
            return
        if root is None:
            root = await self._create_root(ctx)
            if root is None:
                return
        ctx.root = root
        await self._readme(ctx)
        await self._seed(ctx)
        if ctx.host.round_complete:
            await self._marks(ctx)
        await self._due(ctx)
        await self._prune(ctx)
        await self._scan_page(ctx)
        await self._audit_page(ctx)

    # -- step 0: the root's loss (§27.9.9, Δ203) -----------------------------------------------------------------------

    async def _root_step(self, ctx: "_Run", root: Optional[MirrorRoot]) -> bool:
        """Step 0: True when the run goes on (with `ctx.root`, None when step 1 is to create the first root). While the
        root-loss mark is set the run stops (Δ217); after a complete round and MIRROR_ROOT_GRACE one read of the root
        decides (`root_decision`). The root's record back, or the root found, clears the mark. Not found confirms the
        loss: the old id joins `previous_root_ids` first (a previous root is opaque, never written, and its later
        deletion triggers nothing), then the ledger is reset and a new root created (`_finish_loss`). A root whose id is
        already in `previous_root_ids` is a confirmed loss whose reset is still under way."""
        if root is None:
            return True
        ctx.root = root
        if root.root_item_id in root.previous_root_ids:
            return await self._finish_loss(ctx)
        if root.root_gone_at is None:
            return True
        rec = await self._record(ctx, root.root_item_id)
        present = rec is not None and rec.state is not RecordState.DELETING
        decision = root_decision(root, record_present=present, round_complete=ctx.host.round_complete, now=ctx.now)
        if decision is RootDecision.CHECK:
            found = await self._item_found(ctx, root.root_item_id, "root", "the root-loss mark waits")
            if found is None:
                return False
            decision = root_decision(root, record_present=present, round_complete=ctx.host.round_complete,
                                     now=ctx.now, change_found=found)
        if decision is RootDecision.WAIT:
            return False
        if decision is RootDecision.CLEAR:
            ctx.host.fence()
            await self._mirror.clear_root_gone(ctx.user_id, root.root_item_id)
            ctx.root = replace(root, root_gone_at=None)
            ctx.host.root_mark_changed(root.root_item_id, False)
            ctx.counters["mirror_root_mark_cleared"] += 1
            logger.info(f"[DriveMirror] user={ctx.user_id[:8]} the root is there: root-loss mark cleared")
            return True
        ctx.counters["mirror_root_lost"] += 1
        logger.warning(f"[DriveMirror] user={ctx.user_id[:8]} the root is gone after the grace and a not-found read: "
                       f"archived documents dropped, active ones reset, a new root follows")
        await self._save_root(ctx, replace(root, previous_root_ids=(*root.previous_root_ids, root.root_item_id)))
        ctx.host.root_mark_changed(root.root_item_id, False)
        return await self._finish_loss(ctx)

    async def _item_found(self, ctx: "_Run", item_id: str, what: str, outcome: str) -> Optional[bool]:
        """One `get_change` that confirms an irreversible decision (a read): True found, False not found, None when the
        read failed — the caller then waits, since only a not-found answer may confirm a loss (the root's, §27.9.9; a
        folder's whose listing answered 404, before anything beneath it is discarded)."""
        self._take_read(ctx)
        try:
            await self._drive.get_change(ctx.handle, item_id)
            return True
        except DriveItemNotFoundError:
            return False
        except (_SYSTEMIC + (_StopStep,) + ctx.stops):
            raise
        except _CHARGED as exc:
            logger.warning(f"[DriveMirror] user={ctx.user_id[:8]} {what} read failed ({type(exc).__name__}): "
                           f"{outcome}")
            ctx.counters[f"mirror_{what}_read_failed"] += 1
            return None
        except Exception as exc:
            _log_unexpected(ctx.user_id, f"{what} read", exc, outcome)
            ctx.counters["mirror_errors"] += 1
            return None

    async def _finish_loss(self, ctx: "_Run") -> bool:
        """A confirmed loss: the ledger reset (resumed across ticks), then a new root under the name rule, carrying
        `previous_root_ids`. False until both are done; nothing else of the step runs before."""
        if not await self._reset_ledger(ctx):
            return False
        hold = ctx.need_root.quota_hold_until
        if hold is not None and ctx.now < hold:
            return False                                     # a full drive: no root create until the hold ends
        return await self._create_root(ctx, base=ctx.need_root) is not None

    async def _reset_ledger(self, ctx: "_Run") -> bool:
        """The reset after a confirmed loss (Δ200), one unit: it never skips a document, since a document left pointing
        at an item of the previous root could be written by the due step. An unexpected error ends it for this tick,
        and it resumes from its cursor. True when every document of the drive is reset."""
        try:
            return await self._reset_pages(ctx)
        except (_SYSTEMIC + (_StopStep,) + ctx.stops):
            raise
        except Exception as exc:
            _log_unexpected(ctx.user_id, "root-loss reset", exc, "the reset resumes next tick")
            ctx.counters["mirror_errors"] += 1
            return False

    async def _reset_pages(self, ctx: "_Run") -> bool:
        """Pages of the drive's file documents, then of its folder and archive documents, each reset by
        `lost_root_reset`; the position is kept in `scan_cursor` ("f|<key>", "d|<key>"; the scan does not run while the
        reset is under way, and the new root starts it again)."""
        phase, _, after = ctx.need_root.scan_cursor.partition("|")
        if phase not in (_RESET_FILES, _RESET_FOLDERS):
            phase, after = _RESET_FILES, ""
        while True:
            self._check_time(ctx)
            if phase == _RESET_FILES:
                page = await self._mirror.scan_page(ctx.user_id, ctx.drive_identity, after or None, RESET_PAGE)
            else:
                page = await self._mirror.folder_docs(ctx.user_id, ctx.drive_identity, after or None, RESET_PAGE)
            await self._reset_docs(ctx, [d for d in page if d.drive_identity == ctx.drive_identity])
            if len(page) >= RESET_PAGE:
                after = page[-1].key
            elif phase == _RESET_FILES:
                phase, after = _RESET_FOLDERS, ""
            else:
                return True
            await self._save_root(ctx, replace(ctx.need_root, scan_cursor=f"{phase}|{after}"))

    async def _reset_docs(self, ctx: "_Run", docs: Sequence[MirrorDoc]) -> None:
        """One page: the records of the files to create again are made due FIRST (a crash after that re-runs the reset of
        these documents, which still hold their old items), then the documents are written or deleted."""
        plans = [(doc, lost_root_reset(doc)) for doc in docs]
        files = [doc.source_item_id for doc, new in plans if new is not None and new != doc and doc.kind == KIND_FILE]
        recs = await self._records(ctx, files)
        await self._commit(ctx, [due_again(r) for r in recs.values() if r.mirrored_digest != REDO_DIGEST],
                           done=False)
        for doc, new in plans:
            if new is None:
                ctx.host.fence()
                await self._mirror.delete_doc(ctx.user_id, doc.key)
                ctx.counters["mirror_reset_dropped"] += 1
            elif new != doc:
                await self._save_doc(ctx, new)
                ctx.counters["mirror_reset"] += 1

    # -- budgets, fence, small reads -------------------------------------------------------------------------------

    def _take_op(self, ctx: "_Run") -> None:
        if ctx.host.ops_left() <= 0 or ctx.host.time_left() <= 0:
            raise _StopStep()
        ctx.host.fence()
        ctx.host.spend_op()
        ctx.counters["mirror_ops"] += 1

    def _take_read(self, ctx: "_Run") -> None:
        if ctx.host.reads_left() <= 0 or ctx.host.time_left() <= 0:
            raise _StopStep()
        ctx.host.spend_read()
        ctx.counters["mirror_reads"] += 1

    def _check_time(self, ctx: "_Run") -> None:
        if ctx.host.time_left() <= 0:
            raise _StopStep()

    async def _save_doc(self, ctx: "_Run", doc: MirrorDoc) -> None:
        ctx.host.fence()
        await self._mirror.save_doc(doc)

    async def _save_root(self, ctx: "_Run", root: MirrorRoot) -> None:
        ctx.host.fence()
        await self._mirror.save_root(root)
        ctx.root = root

    async def _commit(self, ctx: "_Run", records: Sequence[DriveIndexRecord], *, done: bool = True) -> None:
        """Records written through the tick; `done`: settled for this run (a stamp, a charge), so the queue's copy read
        earlier is not acted on again. A seeded or woken record is not done: it is due now."""
        if not records:
            return
        ctx.host.fence()
        await ctx.host.commit(records)
        for rec in records:
            ctx.recs[rec.item_id] = mirror_plan(rec)
            if done:
                ctx.done.add(rec.item_id)
                if rec.mirror_retry_at is None:                # settled with no hold: its ladder starts again
                    self._held_attempts.pop((ctx.handle.connection_id, rec.item_id), None)

    async def _records(self, ctx: "_Run", item_ids: Sequence[str]) -> Dict[str, DriveIndexRecord]:
        missing = [i for i in dict.fromkeys(item_ids) if i and i not in ctx.recs]
        if missing:
            found = await self._repo.get_records(ctx.handle.scope, missing)
            for item_id in missing:
                ctx.recs[item_id] = found.get(item_id)
        return {i: r for i in item_ids if (r := ctx.recs.get(i)) is not None}

    async def _record(self, ctx: "_Run", item_id: str) -> Optional[DriveIndexRecord]:
        return (await self._records(ctx, [item_id])).get(item_id)

    async def _doc(self, ctx: "_Run", key: str) -> Optional[MirrorDoc]:
        doc = (await self._mirror.get_docs(ctx.user_id, [key])).get(key)
        if doc is not None and doc.drive_identity != ctx.drive_identity:
            raise _OtherDrive(key)        # the key carries the drive: a mismatch is a corrupt document, never overwritten
        return doc

    async def _doc_by_item(self, ctx: "_Run", mirror_item_id: str) -> Optional[MirrorDoc]:
        doc = await self._mirror.by_mirror_item(ctx.user_id, mirror_item_id)
        return doc if doc is not None and doc.drive_identity == ctx.drive_identity else None

    async def _area_root_id(self, ctx: "_Run") -> str:
        if ctx.area_root_id is None:
            state = await self._repo.get_sync_state(ctx.handle.scope)
            if state is not None and state.root_item_id:
                ctx.area_root_id = state.root_item_id
            else:
                self._take_read(ctx)
                ctx.area_root_id = (await self._drive.get_root(ctx.handle)).item_id
        return ctx.area_root_id

    async def _area_path(self, ctx: "_Run") -> str:
        if ctx.area_path is None:
            self._take_read(ctx)
            ctx.area_path = await self._drive.area_path(ctx.handle)
        return ctx.area_path

    async def _root_path(self, ctx: "_Run") -> str:
        """The root's current path below the App Folder, from its record (a rename or move of the root is followed);
        the creation name when the root has no record yet."""
        if ctx.root_path is None:
            root = ctx.need_root
            names: List[str] = []
            rec = await self._record(ctx, root.root_item_id)
            area_root = await self._area_root_id(ctx)
            seen: Set[str] = set()
            while rec is not None and rec.item_id not in seen and len(names) < MAX_CHAIN_DEPTH:
                seen.add(rec.item_id)
                names.append(rec.name)
                if not rec.parent_id or rec.parent_id == area_root:
                    break
                rec = await self._record(ctx, rec.parent_id)
            ctx.root_path = "/".join(reversed(names)) if names else root.root_name
        return ctx.root_path

    async def _quota_hold(self, ctx: "_Run") -> None:
        ctx.counters["mirror_quota_full"] += 1
        logger.warning(f"[DriveMirror] user={ctx.user_id[:8]} the drive is full: the mirror holds for "
                       f"{MIRROR_QUOTA_HOLD}, nothing charged")
        if ctx.root is not None:
            await self._save_root(ctx, replace(ctx.root, quota_hold_until=ctx.now + MIRROR_QUOTA_HOLD))

    async def _guard(self, ctx: "_Run", label: str, work: Awaitable[None],
                     on_charge: Optional[Callable[[], Awaitable[None]]],
                     on_other: Optional[Callable[[], Awaitable[None]]] = None) -> None:
        """One action as a unit (Δ204): systemic classes and the step's own stop leave; a full drive holds the mirror
        and ends the step; a reset chain is tried again next tick; the per-action classes charge. Anything else is
        logged at ERROR (type, frames and a digest of the message, never the message), counted, and charged like the
        other classes (controller ruling on Δ204, fix round 1): a repeating bug parks its document after
        MIRROR_MAX_ATTEMPTS instead of holding the queue's head every tick. A document of another drive writes nothing
        under its key; `on_other` may hold the record only."""
        try:
            await work
        except _SYSTEMIC + ctx.stops:
            raise
        except _StopStep:
            raise
        except _ChainReset:
            ctx.counters["mirror_chain_reset"] += 1
        except _Outside:
            ctx.counters["mirror_outside_wait"] += 1        # nothing written, nothing charged: tried again next tick
        except _OtherDrive:
            ctx.counters["mirror_other_drive"] += 1
            logger.error(f"[DriveMirror] user={ctx.user_id[:8]} {label}: its ledger document belongs to another "
                         f"drive; nothing written under its key")
            if on_other is not None:
                await on_other()
        except DriveStorageFull:
            await self._quota_hold(ctx)
            raise _StopStep()
        except _CHARGED as exc:
            logger.warning(f"[DriveMirror] user={ctx.user_id[:8]} {label}: {type(exc).__name__}, charged")
            ctx.counters["mirror_charged"] += 1
            if on_charge is not None:
                await on_charge()
        except Exception as exc:
            _log_unexpected(ctx.user_id, label, exc)
            ctx.counters["mirror_errors"] += 1
            if on_charge is not None:
                await on_charge()

    # -- step 1: the root and its read-me --------------------------------------------------------------------------

    async def _create_root(self, ctx: "_Run", base: Optional[MirrorRoot] = None) -> Optional[MirrorRoot]:
        """`_Transcripts`, else the next candidate name, under the App Folder root's item id (never
        `special/approot/children`, S1). Each candidate is read first (`child_by_name`, a read): a taken name costs no
        write. The root document is written the moment Graph answers, before any other mirror write. A name held by
        anything else is never written into (never adopted, Δ116), a previous root restored at that name included.
        With all six taken, one WARNING per tick and no write: nothing can hold a back-off, since no root document
        exists yet. `base`: the document of a confirmed loss, whose `previous_root_ids` and seed state carry over."""
        try:
            area_root = await self._area_root_id(ctx)
            for name in root_name_candidates():
                self._take_read(ctx)
                if await self._drive.child_by_name(ctx.handle, area_root, name) is not None:
                    continue
                self._take_op(ctx)
                try:
                    item = await self._drive.create_folder(ctx.handle, area_root, name)
                except DriveNameConflictError:
                    continue
                if base is None:
                    root = MirrorRoot(user_id=ctx.user_id, drive_identity=ctx.drive_identity, root_item_id=item.item_id,
                                      root_name=name, created_at=ctx.now)
                else:
                    root = replace(base, root_item_id=item.item_id, root_name=name, created_at=ctx.now,
                                   readme_item_id="", readme_ctag="", readme_pending_at=None,
                                   readme_blocked_at=None, root_gone_at=None,
                                   scan_cursor="",
                                   audited_at=None, audit_folder_key="", audit_page_cursor="", orphans_reported=False,
                                   quota_hold_until=None)
                await self._save_root(ctx, root)
                ctx.root_created = True
                ctx.counters["mirror_root_created"] += 1
                logger.info(f"[DriveMirror] user={ctx.user_id[:8]} root created "
                            f"(candidate {root_name_candidates().index(name) + 1})")
                return root
        except (_SYSTEMIC + (_StopStep,) + ctx.stops):
            raise
        except DriveStorageFull:
            ctx.counters["mirror_quota_full"] += 1
            logger.warning(f"[DriveMirror] user={ctx.user_id[:8]} the drive is full: no root this tick")
            return None
        except _CHARGED as exc:
            logger.warning(f"[DriveMirror] user={ctx.user_id[:8]} root creation failed: {type(exc).__name__}")
            ctx.counters["mirror_charged"] += 1
            return None
        except Exception as exc:
            _log_unexpected(ctx.user_id, "root creation", exc, "no root this tick")
            ctx.counters["mirror_errors"] += 1
            return None
        ctx.counters["mirror_root_names_taken"] += 1
        logger.warning(f"[DriveMirror] user={ctx.user_id[:8]} every root name is taken by an item that is not ours: "
                       f"no mirror for this user this tick")
        return None

    async def _readme(self, ctx: "_Run") -> None:
        """`_README.md` in the UI language of its creation (§27.9.1): `fail`, with its create intent written first
        (`readme_pending_at`, the Δ191/Δ210 pattern). A run after the one that created the root reads the name first
        and adopts a file there only when an intent of its own is pending and the file was created at or after it
        (minus the skew) with the read-me's size; anything else at the name is the owner's and is never written."""
        root = ctx.need_root
        if root.readme_item_id:
            return
        blocked = root.readme_blocked_at
        if blocked is not None and ctx.now < blocked + timedelta(days=MIRROR_AUDIT_DAYS):
            return                       # the name is the owner's: read again at the audit's cadence, not every tick

        async def write() -> None:
            data = readme_bytes(ctx.host.strings())
            if not ctx.root_created:
                self._take_read(ctx)
                found = await self._drive.child_by_name(ctx.handle, root.root_item_id, README_NAME)
                if found is not None:
                    pending = ctx.need_root.readme_pending_at
                    if pending is not None and _ours(found.is_folder, found.created_at, found.size_bytes, pending,
                                                     len(data), False):
                        await self._save_root(ctx, replace(ctx.need_root, readme_item_id=found.item_id,
                                                           readme_ctag=found.content_version, readme_pending_at=None,
                                                           readme_blocked_at=None))
                        ctx.counters["mirror_adopted"] += 1
                    else:
                        ctx.counters["mirror_foreign"] += 1
                        logger.warning(f"[DriveMirror] user={ctx.user_id[:8]} a foreign item holds the read-me name: "
                                       f"read again in {MIRROR_AUDIT_DAYS} days")
                        await self._save_root(ctx, replace(ctx.need_root, readme_pending_at=None,
                                                           readme_blocked_at=ctx.now))
                    return
            self._take_op(ctx)
            await self._save_root(ctx, replace(ctx.need_root, readme_pending_at=ctx.now))
            try:
                item = await self._drive.upload(ctx.handle, root.root_item_id, README_NAME, data, MD_TYPE,
                                                conflict="fail")
            except DriveNameConflictError:
                ctx.counters["mirror_foreign"] += 1
                logger.warning(f"[DriveMirror] user={ctx.user_id[:8]} the read-me name is taken: read again in "
                               f"{MIRROR_AUDIT_DAYS} days")
                await self._save_root(ctx, replace(ctx.need_root, readme_pending_at=None, readme_blocked_at=ctx.now))
                return
            await self._save_root(ctx, replace(ctx.need_root, readme_item_id=item.item_id, readme_ctag=item.content_version,
                                               readme_pending_at=None, readme_blocked_at=None))
            ctx.counters["mirror_readme_written"] += 1

        await self._guard(ctx, "readme", write(), None)

    # -- step 2: the seed page --------------------------------------------------------------------------------------

    async def _seed(self, ctx: "_Run") -> None:
        """One page of the records whose transcript is current, made due by `mirror_plan` (§27.9.4). The cursor carries
        the version it was started under, so a bump in the middle of a seed starts it again."""
        root = ctx.need_root
        target = seed_version()
        if root.seed_version == target:
            return
        prefix, _, after = root.seed_cursor.partition("|")
        start = after if prefix == target and after else None
        page = await self._repo.transcript_records(ctx.handle.scope, "current", MIRROR_SEED_PAGE, after=start)
        changed = [planned for rec in page if (planned := mirror_plan(rec)) != rec]
        await self._commit(ctx, changed, done=False)
        ctx.counters["mirror_seeded"] += len(changed)
        if len(page) < MIRROR_SEED_PAGE:
            await self._save_root(ctx, replace(ctx.need_root, seed_version=target, seed_cursor=""))
        else:
            await self._save_root(ctx, replace(ctx.need_root, seed_cursor=f"{target}|{page[-1].item_id}"))

    # -- step 3: marks ---------------------------------------------------------------------------------------------

    async def _marks(self, ctx: "_Run") -> None:
        marks = [m for m in await self._mirror.ready_marks(ctx.user_id, ctx.drive_identity, ctx.now, MARKS_PAGE)
                 if m.drive_identity == ctx.drive_identity]
        marks.sort(key=lambda m: 0 if m.kind == KIND_FILE else 1)      # stable: files before the folders above them
        for doc in marks:
            self._check_time(ctx)

            async def charge(doc: MirrorDoc = doc) -> None:
                """A failed mark is held (`mark_ready_at` later), so it is not ready again every tick. Also the
                `on_other` path: a mark whose action met another drive's document under a key it needs is held the
                same way, on its own document (of this drive: `ready_marks` is drive-scoped)."""
                try:
                    fresh = await self._doc(ctx, doc.key) or doc
                except _OtherDrive:
                    return                          # the key is another drive's now: never written, never ready here
                new = charged_doc(fresh, ctx.now)
                if new.failed_at is not None and fresh.failed_at is None:
                    ctx.counters["mirror_failed"] += 1
                held_until = new.failed_at + plan.MIRROR_FAILED_RETRY if new.failed_at is not None else new.retry_at
                await self._save_doc(ctx, mark_held(new, held_until) if held_until is not None else new)

            await self._guard(ctx, f"mark {_kind(doc.key)}", self._mark(ctx, doc), charge, charge)

    async def _mark(self, ctx: "_Run", doc: MirrorDoc) -> None:
        rec = await self._record(ctx, doc.source_item_id)
        present = rec is not None and rec.state is not RecordState.DELETING
        reobserving = await self._ancestor_reobserving(ctx, doc.source_parent_id)
        decision = mark_decision(doc, record_present=present, ancestor_reobserving=reobserving, now=ctx.now)
        if decision is Decision.CHECK:
            self._take_read(ctx)
            try:
                await self._drive.get_change(ctx.handle, doc.source_item_id)
                found = True
            except DriveItemNotFoundError:
                found = False                       # Δ211: read as "gone" for a source the index no longer holds
            decision = mark_decision(doc, record_present=present, ancestor_reobserving=reobserving, now=ctx.now,
                                     change_found=found)
        if decision is Decision.CLEAR:
            ctx.host.fence()
            await self._mirror.clear_mark(ctx.user_id, doc.key)
            ctx.counters["mirror_cleared"] += 1
        elif decision is Decision.WAIT:
            ctx.host.fence()
            await self._mirror.hold_mark(ctx.user_id, doc.key, ctx.now + ANCESTOR_HOLD)
        elif decision is Decision.DETACH:
            await self._detach(ctx, doc)
        elif decision is Decision.ARCHIVE:
            if doc.kind == KIND_FILE:
                await self._archive(ctx, doc)
            else:
                await self._gone_folder(ctx, doc)

    async def _ancestor_reobserving(self, ctx: "_Run", parent_id: str) -> bool:
        """An ancestor folder record is still being re-listed (Δ212): a restored large folder."""
        area_root = await self._area_root_id(ctx)
        seen: Set[str] = set()
        while parent_id and parent_id != area_root and parent_id not in seen and len(seen) < MAX_CHAIN_DEPTH:
            seen.add(parent_id)
            rec = await self._record(ctx, parent_id)
            if rec is None:
                return False
            if rec.reobserve_pending:
                return True
            parent_id = rec.parent_id
        return False

    async def _detach(self, ctx: "_Run", doc: MirrorDoc) -> None:
        """`unstored` (Δ190): the file stays where it is and no longer follows renames; the document pins its store
        entry. A document that holds only an intent resolves it first (`_with_item`)."""
        found = await self._with_item(ctx, doc)
        if found is None:
            return
        doc = found
        await self._save_doc(ctx, replace(reset_charges(unmarked(doc)), state=MIRROR_DETACHED, checked_at=ctx.now))
        ctx.counters["mirror_detached"] += 1

    async def _with_item(self, ctx: "_Run", doc: MirrorDoc) -> Optional[MirrorDoc]:
        """A marked document with no mirror item: its intent is resolved once (one read; a file's size is the intent's
        `pending_size`, since the record is gone). Adopted → the document goes on like any other; absent, foreign or no
        intent at all → nothing of ours is on the drive, and the document is deleted (None)."""
        if doc.mirror_item_id:
            return doc
        if doc.pending_op:
            doc = await self._resolve_intent(ctx, doc, None)
            if doc.mirror_item_id:
                return doc
        ctx.host.fence()
        await self._mirror.delete_doc(ctx.user_id, doc.key)
        ctx.counters["mirror_intent_dropped"] += 1
        logger.info(f"[DriveMirror] user={ctx.user_id[:8]} {_kind(doc.key)} document with nothing on the drive and a "
                    f"gone source deleted")
        return None

    async def _gone_folder(self, ctx: "_Run", doc: MirrorDoc) -> None:
        """A gone source folder: its counterpart is pruned once nothing of ours is beneath it (§27.9.5 step 5, Δ197);
        while documents are beneath it the mark waits for them (files are handled before folders) — until the earliest
        time one of them can move (`_children_wait`, Δ240), never a fixed 10 minutes, so a child held for a week (a
        folder kept for the owner's own file, a parked one) does not make its parent ready on every tick; if all of
        them are detached, the folder is detached with them."""
        found = await self._with_item(ctx, doc)
        if found is None:
            return
        doc = found
        kids = await self._mirror.children(ctx.user_id, ctx.drive_identity, doc.mirror_item_id, CHILDREN_MAX)
        if kids and all(k.state == MIRROR_DETACHED for k in kids):
            await self._detach(ctx, doc)
        elif kids:
            ctx.host.fence()
            await self._mirror.hold_mark(ctx.user_id, doc.key, _children_wait(kids, ctx.now))
        elif plan.prune_due(doc, source_present=False, has_child_docs=False):
            ctx.prune.append(doc)
        else:                                       # a detached folder whose source went: kept as it is
            ctx.host.fence()
            await self._mirror.clear_mark(ctx.user_id, doc.key)
            ctx.counters["mirror_cleared"] += 1

    # -- the archive (§27.9.6) --------------------------------------------------------------------------------------

    async def _archive(self, ctx: "_Run", doc: MirrorDoc) -> None:
        """One move into `Archive/<day>/<mirror path>` (the day of `gone_at` in the user's zone), the chain of archive
        folder documents created on demand, then the search keys and the day on the document (Δ213). A temporary
        name left by a stopped swap is not kept: the file is archived under its source's mirror name."""
        found = await self._with_item(ctx, doc)
        if found is None:
            return
        doc = found
        day = local_day(doc.gone_at or ctx.now, ctx.host.tz_name)
        active_parts = await self._mirror_parts(ctx, doc)
        placement = archive_place(day, active_parts, area_path=await self._area_path(ctx),
                                  root_path=await self._root_path(ctx))
        target = await self._archive_chain(ctx, day, placement)
        old_parent = doc.mirror_parent_id
        in_day = len(placement.parts) == 3 and placement.parts[2] != LONG_PATHS_NAME
        moved = await self._place(ctx, target, LEVEL_DAY if in_day else LEVEL_OTHER,
                                  Sibling(doc.source_item_id, _archive_name(doc), False), doc, None, frozen=True)
        if moved is None:                                     # the file is gone: the owner's deletion
            ctx.host.fence()
            await self._mirror.delete_doc(ctx.user_id, doc.key)
            ctx.counters["mirror_archive_lost"] += 1
            return
        stored = await self._repo.get_transcript(ctx.user_id, doc.content_key) if doc.content_key else None
        archived = replace(reset_charges(unmarked(moved)), state=MIRROR_ARCHIVED, archived_at=ctx.now,
                           archive_day=day, archive_keys=archive_word_keys(stored) if stored is not None else (),
                           name_keys=name_keys_v2(doc.source_name), pending_op="", pending_name="",
                           pending_parent_id="", pending_at=None, pending_size=0)
        await self._save_doc(ctx, archived)
        ctx.counters["mirror_archived"] += 1
        await self._settle_after(ctx, old_parent)

    async def _mirror_parts(self, ctx: "_Run", doc: MirrorDoc) -> Tuple[str, ...]:
        """The file's path below the root as the mirror has it (its folders' and its own mirror names)."""
        root_id = ctx.need_root.root_item_id
        names = [_archive_name(doc)]
        parent = doc.mirror_parent_id
        seen: Set[str] = set()
        while parent and parent != root_id:
            if parent in seen or len(seen) >= MAX_CHAIN_DEPTH:
                raise _Unplaceable("mirror chain cycle")
            seen.add(parent)
            folder = await self._doc_by_item(ctx, parent)
            if folder is None:
                raise _Unplaceable("mirror chain broken")
            names.append(folder.mirror_name)
            parent = folder.mirror_parent_id
        if not parent:
            raise _Unplaceable("mirror chain has no parent")
        return tuple(reversed(names))

    async def _archive_chain(self, ctx: "_Run", day: str, placement: Placement) -> str:
        """`Archive`, the day folder, then the folders of the mirror path (or the day's `Long paths`), each an archive
        folder document keyed by (drive, day, path). Returns the target folder's item id."""
        root_id = ctx.need_root.root_item_id
        parent = await self._fixed_folder(ctx, archive_folder_key(ctx.user_id, ctx.drive_identity, "", ARCHIVE_NAME),
                                          root_id, ARCHIVE_NAME)
        parent = await self._archive_folder(ctx, day, "", parent, day, LEVEL_OTHER)
        dirs = placement.parts[2:-1]
        if placement.long_path or tuple(dirs) == (LONG_PATHS_NAME,):     # a file of the active `Long paths` too
            return await self._fixed_folder(ctx, archive_folder_key(ctx.user_id, ctx.drive_identity, day, LONG_PATHS_NAME),
                                            parent, LONG_PATHS_NAME)
        path: List[str] = []
        for name in dirs:
            level = LEVEL_DAY if not path else LEVEL_OTHER
            path.append(name)
            parent = await self._archive_folder(ctx, day, "/".join(path), parent, name, level)
        return parent

    async def _archive_folder(self, ctx: "_Run", day: str, path: str, parent: str, name: str, level: str) -> str:
        key = archive_folder_key(ctx.user_id, ctx.drive_identity, day, path)
        doc = await self._doc(ctx, key) or _archive_doc(ctx, key, name)
        if doc.pending_op and not doc.mirror_item_id:
            doc = await self._resolve_intent(ctx, doc, None)
        if doc.mirror_item_id:
            return doc.mirror_item_id
        placed = await self._place(ctx, parent, level, Sibling(doc.source_item_id, name, True), doc,
                                   lambda d, p, n: self._create_folder(ctx, d, p, n), frozen=True)
        if placed is None:
            raise _ChainReset()
        return placed.mirror_item_id

    async def _fixed_folder(self, ctx: "_Run", key: str, parent: str, name: str) -> str:
        """A reserved folder (`Archive`, `Long paths`): always its fixed name; a foreign item holding it blocks the
        action (charged) and is never written into."""
        doc = await self._doc(ctx, key) or _archive_doc(ctx, key, name)
        if doc.pending_op and not doc.mirror_item_id:
            doc = await self._resolve_intent(ctx, doc, None)
        if doc.mirror_item_id:
            return doc.mirror_item_id
        if fold(name) in ctx.taken_in(parent):
            raise DriveNameConflictError("a reserved folder name is held by a foreign item")
        try:
            created = await self._create_folder(ctx, doc, parent, name)
        except _NameTaken:
            ctx.taken_in(parent).add(fold(name))
            ctx.counters["mirror_foreign"] += 1
            raise DriveNameConflictError("a reserved folder name is held by a foreign item")
        return created.mirror_item_id

    # -- step 4: due records ---------------------------------------------------------------------------------------

    async def _due(self, ctx: "_Run") -> None:
        wakeups = await self._repo.mirror_wakeups(ctx.handle.scope, ctx.now, WAKEUP_PAGE)
        if wakeups:
            await self._commit(ctx, [woken(r) for r in wakeups], done=False)
            ctx.counters["mirror_woken"] += len(wakeups)
        seen: Set[str] = set()
        while True:
            page = await self._repo.mirror_queue(ctx.handle.scope, QUEUE_PAGE)
            fresh = [r for r in page if r.item_id not in seen and r.item_id not in ctx.done]
            if not fresh:
                return
            for rec in fresh:
                seen.add(rec.item_id)
                if rec.item_id in ctx.done:
                    continue
                self._check_time(ctx)
                ctx.recs[rec.item_id] = rec
                await self._guard(ctx, f"due {'folder' if rec.is_folder else 'file'} {_hid(rec.item_id)}",
                                  self._due_record(ctx, rec), functools.partial(self._charge, ctx, rec),
                                  functools.partial(self._hold_record, ctx, rec))
            if len(page) < QUEUE_PAGE:
                return

    async def _charge(self, ctx: "_Run", rec: DriveIndexRecord) -> None:
        key = _ledger_key(ctx, KIND_FOLDER if rec.is_folder else KIND_FILE, rec)
        try:
            doc = await self._doc(ctx, key) or (_folder_doc(ctx, rec) if rec.is_folder else _file_doc(ctx, rec))
        except (_SYSTEMIC + (_StopStep,) + ctx.stops):
            raise
        except Exception as exc:          # another drive's document, or one that cannot be decoded
            logger.warning(f"[DriveMirror] user={ctx.user_id[:8]} due {_hid(rec.item_id)}: its document cannot be "
                           f"read ({type(exc).__name__}); only the record is held")
            await self._hold_record(ctx, rec)     # never left due: one bad document must not hold the queue's head
            return
        new_doc, new_rec = charged(doc, ctx.recs.get(rec.item_id) or rec, ctx.now)
        if new_doc.failed_at is not None and doc.failed_at is None:
            ctx.counters["mirror_failed"] += 1
        await self._save_doc(ctx, new_doc)
        await self._commit(ctx, [new_rec])

    async def _hold_record(self, ctx: "_Run", rec: DriveIndexRecord) -> None:
        """The record's hold only, for an action that may write nothing under its document's key (another drive's
        document, or one that cannot be decoded): off the queue, so it never holds the queue's head. The document cannot
        carry the attempts, so they are counted here (`_held_attempts`, process memory, like the tick's root-setup hold,
        Δ227): the same ladder as a charged document's — a growing back-off, then parked for MIRROR_FAILED_RETRY at
        MIRROR_MAX_ATTEMPTS — instead of the first back-off every time, which made such a record ripe again (and the
        reserve taken) for good (final review, deploy Minor 3). A restart starts the ladder again: bounded."""
        slot = (ctx.handle.connection_id, rec.item_id)
        attempts = self._held_attempts.get(slot, 0)
        self._held_attempts[slot] = attempts + 1
        base = replace(_folder_doc(ctx, rec) if rec.is_folder else _file_doc(ctx, rec), attempts=attempts)
        await self._commit(ctx, [charged(base, ctx.recs.get(rec.item_id) or rec, ctx.now)[1]])

    async def _due_record(self, ctx: "_Run", rec: DriveIndexRecord) -> None:
        if rec.is_folder:
            await self._folder_record(ctx, rec)
        else:
            await self._file_record(ctx, rec)

    async def _folder_record(self, ctx: "_Run", rec: DriveIndexRecord) -> None:
        """A source folder that has a counterpart: the chain above it and the folder itself placed (one move for a
        rename or a move of the folder, Δ113), then its record stamped."""
        chain = await self._source_chain(ctx, rec)
        _, wrote = await self._ensure_chain(ctx, [*chain, rec])
        if rec.item_id not in ctx.done:
            if not wrote:
                self._take_stamp(ctx)
            await self._commit(ctx, [stamped(rec)])
            ctx.counters["mirror_stamped"] += 1

    def _take_stamp(self, ctx: "_Run") -> None:
        if ctx.host.stamps_left() <= 0 or ctx.host.time_left() <= 0:
            raise _StopStep()
        ctx.host.spend_stamp()

    async def _file_record(self, ctx: "_Run", rec: DriveIndexRecord) -> None:
        stored = await self._stored(ctx, rec)
        chain = await self._source_chain(ctx, rec)
        source_path = "/".join(a.name for a in chain)
        data = render_file(stored, rec, ctx.host.strings(), source_path=source_path, recorded_on=rec.effective_day)
        work = _FileWork(rec, chain, source_path, data, rendered_hash(data))
        key = _ledger_key(ctx, KIND_FILE, rec)
        doc = reset_charges(await self._doc(ctx, key) or _file_doc(ctx, rec), mirror_digest(rec))
        if doc.pending_op and not doc.mirror_item_id:
            doc = await self._resolve_intent(ctx, doc, len(data))
        for _ in range(MAX_ACTIONS):
            doc, finished = await self._file_step(ctx, work, doc)
            if finished:
                break
        else:
            raise ValueError("the file's actions did not settle")
        for folder in dict.fromkeys(work.freed):
            await self._settle_after(ctx, folder)

    async def _stored(self, ctx: "_Run", rec: DriveIndexRecord) -> StoredTranscript:
        stored = await self._repo.get_transcript(ctx.user_id, rec.transcript_key) if rec.transcript_key else None
        if stored is not None:
            return stored
        if rec.transcript_stub:
            return StoredTranscript(rec.transcript_key, None, stub=rec.transcript_stub)
        raise ValueError("the stored transcript is missing")

    async def _file_step(self, ctx: "_Run", work: "_FileWork", doc: MirrorDoc) -> Tuple[MirrorDoc, bool]:
        """One action on a due recording (§27.9.5 step 4); True once the record is stamped."""
        rec = work.rec
        name = mirror_file_name(rec.name)
        long = bool(doc.long_path_source) and doc.long_path_source == placement_source(rec)
        if not long:
            folder_names = [await self._chain_name(ctx, a) for a in work.chain]
            long = not fits(await self._area_path(ctx), await self._root_path(ctx), [*folder_names, name])
        if long:
            parent = await self._fixed_folder(ctx, archive_folder_key(ctx.user_id, ctx.drive_identity, "", LONG_PATHS_NAME),
                                              ctx.need_root.root_item_id, LONG_PATHS_NAME)
            level = LEVEL_OTHER
            prefix_parts: Tuple[str, ...] = (LONG_PATHS_NAME,)
        else:
            try:
                parent, wrote = await self._ensure_chain(ctx, work.chain)
            except DrivePathTooLong:
                return await self._re_place(ctx, work, doc), False
            work.wrote = work.wrote or wrote
            level = LEVEL_ROOT if not work.chain else LEVEL_OTHER
            prefix_parts = tuple([await self._chain_name(ctx, a) for a in work.chain])
        sib = Sibling(rec.item_id, name, False)
        assigned = await self._assign(ctx, parent, level, sib, doc, frozen=False)
        action = file_action(rec, doc, work.hash, Placement((*prefix_parts, assigned[rec.item_id]), long))
        if action in (Action.REPLACE, Action.STAMP_ONLY) and doc.mirror_parent_id != parent:
            action = Action.MOVE
        if action is Action.STAMP_ONLY:
            if not work.wrote:
                self._take_stamp(ctx)
            final = _snapshot(replace(reset_charges(unmarked(doc)), state=MIRROR_ACTIVE,
                                      render_version=plan.RENDER_VERSION), work)
            if final != doc:
                await self._save_doc(ctx, final)
            await self._commit(ctx, [stamped(rec)])
            ctx.counters["mirror_stamped"] += 1
            return final, True
        if action is Action.REATTACH:
            ctx.counters["mirror_reattached"] += 1
            return await self._saved(ctx, replace(unmarked(doc), state=MIRROR_ACTIVE, checked_at=None)), False
        if action is Action.REPLACE:
            await self._ensure_inside(ctx, doc.mirror_item_id)       # the file itself (moved out: an owner's record)
            await self._ensure_inside(ctx, doc.mirror_parent_id)     # and its folder
            self._take_op(ctx)
            try:
                item = await self._drive.replace_content(ctx.handle, doc.mirror_item_id, work.data, MD_TYPE)
            except DriveItemNotFoundError:            # no read here: `_item_gone` says why
                return await self._item_gone(ctx, doc, work), False
            work.wrote = True
            ctx.counters["mirror_replaced"] += 1
            return await self._saved(ctx, _snapshot(replace(doc, rendered_hash=work.hash, mirror_ctag=item.content_version,
                                                            render_version=plan.RENDER_VERSION,
                                                            render_language=ctx.host.language), work)), False
        # CREATE, MOVE, RESTORE: a placement in `parent`
        old_parent, was_archived = doc.mirror_parent_id, doc.state == MIRROR_ARCHIVED
        try:
            placed = await self._place(ctx, parent, level, sib, doc,
                                       lambda d, p, n: self._upload(ctx, work, d, p, n), frozen=False)
        except DrivePathTooLong:
            if long:
                raise
            return await self._re_place(ctx, work, doc), False
        work.wrote = True
        if placed is None:                                    # the file is gone: created again from the store
            return await self._item_gone(ctx, doc, work), False
        placed = replace(placed, source_name=rec.name, source_parent_id=rec.parent_id, state=MIRROR_ACTIVE)
        if action is Action.CREATE:
            ctx.counters["mirror_created"] += 1
        elif was_archived:
            placed = replace(placed, archived_at=None, archive_day="", archive_keys=(), name_keys=())
            ctx.counters["mirror_restored"] += 1
        else:
            ctx.counters["mirror_moved"] += 1
        if old_parent and old_parent != parent and not was_archived and action is not Action.CREATE:
            work.freed.append(old_parent)
        elif action is Action.MOVE and old_parent == parent:
            work.freed.append(parent)                         # a rename frees the old name in the same folder
        return await self._saved(ctx, placed), False

    async def _re_place(self, ctx: "_Run", work: "_FileWork", doc: MirrorDoc) -> MirrorDoc:
        """DrivePathTooLong (Δ204, Δ216): the file goes to `Long paths` once; the decision is kept on the document for
        this source placement, so the next failure is charged and a suffixed name there is never moved back."""
        ctx.counters["mirror_long_path"] += 1
        return await self._saved(ctx, replace(doc, long_path_source=placement_source(work.rec)))

    async def _item_gone(self, ctx: "_Run", doc: MirrorDoc, work: "_FileWork") -> MirrorDoc:
        """The document's file is not on the drive: a PATCH's 404 confirmed by a read of the item (`_patch_404`), or a
        `replace_content` 404 taken as it is (no read: a re-create under a missing root then meets `_create_404`, which
        reads the root). An active file, or an archived one whose source is back, is created again from the store
        (§27.9.5, §27.9.6)."""
        ctx.counters["mirror_recreated"] += 1
        return await self._saved(ctx, replace(doc, mirror_item_id="", mirror_parent_id="", mirror_name="",
                                              mirror_ctag="", rendered_hash="", state=MIRROR_ACTIVE, archived_at=None,
                                              archive_day="", archive_keys=(), name_keys=()))

    async def _saved(self, ctx: "_Run", doc: MirrorDoc) -> MirrorDoc:
        await self._save_doc(ctx, doc)
        return doc

    async def _chain_name(self, ctx: "_Run", folder: DriveIndexRecord) -> str:
        """The name a source folder has in the mirror: its document's, else the source's."""
        doc = await self._doc(ctx, _ledger_key(ctx, KIND_FOLDER, folder))
        return doc.mirror_name if doc is not None and doc.mirror_item_id else folder.name

    async def _source_chain(self, ctx: "_Run", rec: DriveIndexRecord) -> List[DriveIndexRecord]:
        """The folder records above `rec`, top first, up to the App Folder root; a missing, deleting or non-folder
        ancestor, or a cycle, cannot be placed (charged)."""
        area_root = await self._area_root_id(ctx)
        chain: List[DriveIndexRecord] = []
        parent_id, seen = rec.parent_id, {rec.item_id}
        while parent_id != area_root:
            if not parent_id or parent_id in seen or len(chain) >= MAX_CHAIN_DEPTH:
                raise _Unplaceable("the source chain cannot be resolved")
            parent = await self._record(ctx, parent_id)
            if parent is None or not parent.is_folder or parent.state is RecordState.DELETING:
                raise _Unplaceable("an ancestor is not recorded")
            seen.add(parent_id)
            chain.append(parent)
            parent_id = parent.parent_id
        return list(reversed(chain))

    async def _ensure_chain(self, ctx: "_Run", chain: Sequence[DriveIndexRecord]) -> Tuple[str, bool]:
        """Walk the source folders top-down (§27.9.5 step 4): a folder document that is not where its record says is
        moved first, a missing one created (its record stamped, Δ113); returns the last folder's mirror id and whether
        a Graph write happened."""
        parent, level, wrote = ctx.need_root.root_item_id, LEVEL_ROOT, False
        for folder in chain:
            key = _ledger_key(ctx, KIND_FOLDER, folder)
            doc = await self._doc(ctx, key) or _folder_doc(ctx, folder)
            if doc.pending_op and not doc.mirror_item_id:
                doc = await self._resolve_intent(ctx, doc, None)
            if doc.gone_pending or doc.state != MIRROR_ACTIVE:     # its record exists: the mark and a detach end
                doc = await self._saved(ctx, replace(unmarked(doc), state=MIRROR_ACTIVE))
            if not doc.mirror_item_id or not _folder_placed(doc, folder, parent):
                creating = not doc.mirror_item_id
                old_parent = doc.mirror_parent_id
                placed = await self._place(ctx, parent, level, Sibling(folder.item_id, folder.name, True), doc,
                                           lambda d, p, n: self._create_folder(ctx, d, p, n), frozen=False)
                if placed is None:                              # the folder is gone: created next time
                    await self._saved(ctx, _reset_folder(doc))
                    raise _ChainReset()
                doc = await self._saved(ctx, replace(placed, source_name=folder.name,
                                                     source_parent_id=folder.parent_id))
                wrote = True
                ctx.counters["mirror_folders_created" if creating else "mirror_moved"] += 1
                if not creating and old_parent:
                    await self._settle_after(ctx, old_parent)
                await self._commit(ctx, [stamped(ctx.recs.get(folder.item_id) or folder)])
            parent, level = doc.mirror_item_id, LEVEL_OTHER
        return parent, wrote

    # -- the root's bounds (RFC §27.12: no write outside the root) ------------------------------------------------------

    async def _inside_root(self, ctx: "_Run", folder_id: str) -> Tuple[bool, str]:
        """Is `folder_id` (a folder, or a file of ours) inside the root, by the index records (no Graph call)? A file
        under the root has no record (opaque), so a file of ours with one was moved out and indexed as the owner's. Walks `parent_id` upward through the
        records (read through the run's cache; a verdict of this run is reused): the root → True; the App Folder root,
        or a record whose scope is not `inside_opaque` (a folder of ours the owner moved out is re-scoped by the
        observation that runs before the step) → False. A missing link (no record yet, an unresolved one, a cycle)
        contradicts nothing → True, so a folder not observed yet never stalls the step. Also returns the highest
        folder of the ledger met on the way (the one to read and repair when the answer is False)."""
        root_id = ctx.need_root.root_item_id
        area = await self._area_root_id(ctx)
        cur, seen, culprit = folder_id, [], ""
        while True:
            if not cur or cur == root_id:
                verdict = True
                break
            if cur in ctx.inside:
                verdict = ctx.inside[cur]
                break
            if cur == area:
                verdict = False
                break
            if cur in seen or len(seen) >= MAX_CHAIN_DEPTH:
                verdict = True
                break
            seen.append(cur)
            rec = await self._record(ctx, cur)
            if rec is None or rec.scope is Scope.UNRESOLVED:
                verdict = True
                break
            if await self._doc_by_item(ctx, cur) is not None:
                culprit = cur
            if rec.scope is not Scope.INSIDE_OPAQUE:
                verdict = False
                break
            cur = rec.parent_id
        if verdict:
            for item in seen:
                ctx.inside[item] = True
        return verdict, culprit

    async def _ensure_inside(self, ctx: "_Run", folder_id: str) -> None:
        """Before a write into `folder_id` (a create's parent, a move's destination, a replaced file and its folder, a
        pruned folder): raises `_Outside` unless it is inside the root. On a False answer the highest folder of ours on the
        chain is read once (the mirror's read budget): its record may be stale (the bot moved it back already) — then
        it is inside and the write goes on; really outside — it is moved back (the audit's move-back, or `due_again`
        when the way back is blocked), and the write waits for the next tick unless the move succeeded."""
        inside, culprit = await self._inside_root(ctx, folder_id)
        if inside:
            return
        if culprit and culprit not in ctx.repairing:
            ctx.repairing.add(culprit)
            ctx.inside[culprit] = await self._repair_outside(ctx, culprit)
            inside, _ = await self._inside_root(ctx, folder_id)
            if inside:
                return
        elif culprit:
            ctx.inside.setdefault(culprit, False)
        logger.info(f"[DriveMirror] user={ctx.user_id[:8]} a write's folder is outside the root: it waits")
        raise _Outside()

    async def _repair_outside(self, ctx: "_Run", folder_item_id: str) -> bool:
        """One read of a folder (or file) of ours whose record places it outside the root: the highest ledger folder on
        the chain, up to the first non-opaque record (`_inside_root`'s culprit). True when it is inside after all (a
        stale record) or was moved back now (its own move, or an ancestor's, which `_move_back` ensures first)."""
        self._take_read(ctx)
        try:
            change = await self._drive.get_change(ctx.handle, folder_item_id)
        except DriveItemNotFoundError:
            return False
        parent = change.parent_id
        if parent == ctx.need_root.root_item_id or (parent != folder_item_id and parent not in ctx.repairing
                                                    and (await self._inside_root(ctx, parent))[0]):
            ctx.counters["mirror_outside_stale"] += 1
            return True
        doc = await self._doc_by_item(ctx, folder_item_id)
        if doc is None:
            return False
        ctx.counters["mirror_outside_found"] += 1
        return await self._move_back(ctx, doc, change)

    # -- names and placements ----------------------------------------------------------------------------------------

    async def _assign(self, ctx: "_Run", parent: str, level: str, sib: Sibling, subject: MirrorDoc, *,
                      frozen: bool) -> Dict[str, str]:
        assigned, _, _ = await self._plan_folder(ctx, parent, level, sib, subject, frozen=frozen)
        return assigned

    async def _plan_folder(self, ctx: "_Run", parent: str, level: str, sib: Optional[Sibling],
                           subject: Optional[MirrorDoc], *, frozen: bool
                           ) -> Tuple[Dict[str, str], List[Move], Dict[str, MirrorDoc]]:
        """Names for the folder's documents plus the newcomer (Δ192), and the moves they imply. Movable: the active
        file and folder documents of the folder (wanted names from their records when those are mirror-eligible in the
        same folder, else from the document); fixed: archive folders, detached and archived documents, every document
        of a frozen (archive) folder, and the names foreign items hold this run."""
        kids = await self._mirror.children(ctx.user_id, ctx.drive_identity, parent, CHILDREN_MAX)
        kids = [k for k in kids if k.drive_identity == ctx.drive_identity and (subject is None or k.key != subject.key)]
        movable = [] if frozen else [k for k in kids if k.kind in (KIND_FILE, KIND_FOLDER) and k.state == MIRROR_ACTIVE
                                     and k.mirror_item_id and k.key not in ctx.fixed]
        movable_keys = {k.key for k in movable}
        taken = set(ctx.taken_in(parent)) | {k.mirror_name for k in kids if k.key not in movable_keys and k.mirror_name}
        recs = await self._records(ctx, [k.source_item_id for k in movable])
        siblings = [Sibling(k.source_item_id, _wanted(k, recs.get(k.source_item_id)), k.kind == KIND_FOLDER)
                    for k in movable]
        if sib is not None:
            siblings.append(sib)
        assigned = assign_names(siblings, reserved_names(level), taken)
        moves: List[Move] = []
        by_key: Dict[str, MirrorDoc] = {}
        for k in movable:
            by_key[k.key] = k
            if k.mirror_name != assigned[k.source_item_id]:
                moves.append(Move(k.key, parent, k.mirror_name, parent, assigned[k.source_item_id]))
        if sib is not None and subject is not None:
            by_key[subject.key] = subject
            target = assigned[sib.item_id]
            if not subject.mirror_item_id:
                moves.append(Move(subject.key, None, None, parent, target))
            elif (subject.mirror_parent_id, subject.mirror_name) != (parent, target):
                moves.append(Move(subject.key, subject.mirror_parent_id, subject.mirror_name, parent, target))
        return assigned, order_moves(moves), by_key

    async def _place(self, ctx: "_Run", parent: str, level: str, sib: Sibling, subject: MirrorDoc,
                     create: Optional[Callable[[MirrorDoc, str, str], Awaitable[MirrorDoc]]], *,
                     frozen: bool) -> Optional[MirrorDoc]:
        """Put `subject` at its assigned name in `parent`: the yields first, through `order_moves`, then the subject's
        own create or move. A 409 makes the name taken and the names are assigned again. Returns the subject's
        document after the last write, or None when its item is not on the drive (a PATCH 404 confirmed by a read)."""
        for _ in range(NAME_RETRIES):
            _, moves, docs = await self._plan_folder(ctx, parent, level, sib, subject, frozen=frozen)
            try:
                for move in moves:
                    if move.key == subject.key:
                        if move.is_create:
                            if create is None:
                                raise ValueError("a create without a create action")
                            subject = await create(subject, parent, move.to_name)
                        else:
                            subject = await self._patch(ctx, subject, move)
                    else:
                        docs[move.key] = await self._patch(ctx, docs[move.key], move, sibling=True)
                return subject
            except _NameTaken as taken:
                ctx.taken_in(taken.parent).add(fold(taken.name))
                ctx.counters["mirror_foreign"] += 1
                continue
            except _SiblingStuck as stuck:
                ctx.fixed.add(stuck.key)
                continue
            except _PatchNotFound as missing:
                gone = await self._patch_404(ctx, missing.doc, missing.parent)
                if missing.doc.key == subject.key:
                    if gone:
                        return None
                    raise ValueError("a move answered not found while the item and its destination exist")
                if gone:
                    await self._sibling_gone(ctx, missing.doc)
                    continue
                raise ValueError("a sibling's move answered not found while it and its destination exist")
        raise DriveNameConflictError("names kept colliding")

    async def _patch(self, ctx: "_Run", doc: MirrorDoc, move: Move, *, sibling: bool = False) -> MirrorDoc:
        """One PATCH (a case-only rename included, Δ216); the document records the drive name after EACH call. The
        destination must be inside the root (`_ensure_inside`); a folder of ours moved there is inside for the run. A
        rename-only PATCH (no new parent: the item stays where it is) also needs the ITEM inside the root (final review
        I1, Δ235): the owner may have moved our file or folder out into a folder of his, which the ledger's parent does
        not show, so `_ensure_inside` reads it and moves it back first (or the rename waits, `_Outside`). This covers the
        subject's own renames and the siblings' (`_place`, `_settle_after`)."""
        await self._ensure_inside(ctx, move.to_parent)
        new_parent = move.to_parent if move.to_parent != doc.mirror_parent_id else None
        if new_parent is None:
            await self._ensure_inside(ctx, doc.mirror_item_id)
        self._take_op(ctx)
        new_name = move.to_name if move.to_name != doc.mirror_name else None
        try:
            item = await self._drive.move(ctx.handle, doc.mirror_item_id, new_parent_id=new_parent,
                                          new_name=new_name)
        except DriveNameConflictError:
            raise _NameTaken(move.to_parent, move.to_name)
        except DriveItemNotFoundError:
            raise _PatchNotFound(doc, move.to_parent)
        except DrivePathTooLong:
            if not sibling:
                raise
            raise _SiblingStuck(doc.key)       # a sibling's own path problem is never the subject's: it stays put
        # a move never changes content: the recorded cTag stays the reference (an owner's edit made before the move would
        # otherwise become it, and never be rewritten; Δ242)
        moved = replace(doc, mirror_parent_id=move.to_parent, mirror_name=move.to_name,
                        mirror_ctag=doc.mirror_ctag or item.content_version)
        if doc.kind != KIND_FILE:
            ctx.inside[doc.mirror_item_id] = True          # placed inside by this run: its record is stale until seen
        if sibling:
            rec = ctx.recs.get(doc.source_item_id)
            if rec is not None and _record_wanted(doc, rec) == move.to_name:
                moved = replace(moved, source_name=rec.name)       # the final move to the record's name
            ctx.counters["mirror_renamed"] += 1
        await self._save_doc(ctx, moved)
        return moved

    async def _patch_404(self, ctx: "_Run", doc: MirrorDoc, dest: str) -> bool:
        """A PATCH answered 404 (§27.9.5): read the item, then the destination. True: the item is gone. A missing
        destination resets that folder's document (`_ChainReset`); both present raises."""
        self._take_read(ctx)
        try:
            await self._drive.get_change(ctx.handle, doc.mirror_item_id)
        except DriveItemNotFoundError:
            return True
        self._take_read(ctx)
        try:
            await self._drive.get_change(ctx.handle, dest)
        except DriveItemNotFoundError:
            await self._reset_folder_item(ctx, dest)
            raise _ChainReset()
        return False

    async def _sibling_gone(self, ctx: "_Run", doc: MirrorDoc) -> None:
        """A sibling's file is not on the drive: its document is reset to create and its record made due."""
        await self._save_doc(ctx, replace(doc, mirror_item_id="", mirror_parent_id="", mirror_name="",
                                          rendered_hash=""))
        rec = await self._record(ctx, doc.source_item_id)
        if rec is not None and mirror_eligible(rec):
            # `due_again`, not "": a record reset with an empty digest would send the mirror no notice when it is
            # deleted later (the tick's `_mirror_notices` keys on the digest)
            await self._commit(ctx, [due_again(rec)], done=False)

    async def _reset_folder_item(self, ctx: "_Run", folder_item_id: str) -> None:
        if folder_item_id == ctx.need_root.root_item_id:
            await self._root_missing(ctx)
        doc = await self._doc_by_item(ctx, folder_item_id)
        if doc is not None:
            await self._save_doc(ctx, _reset_folder(doc))

    async def _root_missing(self, ctx: "_Run") -> None:
        """The root answered not found: the root-loss mark (§27.9.9, decided by Task 21B's step 0) and the step ends."""
        root = ctx.need_root
        ctx.host.fence()
        await self._mirror.mark_root_gone(ctx.user_id, root.root_item_id, ctx.now)
        ctx.root = replace(root, root_gone_at=root.root_gone_at or ctx.now)
        ctx.host.root_mark_changed(root.root_item_id, True)
        ctx.counters["mirror_root_missing"] += 1
        logger.warning(f"[DriveMirror] user={ctx.user_id[:8]} the root answered not found: root-loss mark set")
        raise _StopStep()

    async def _create_404(self, ctx: "_Run", parent: str) -> None:
        """A create answered 404 (§27.9.5): the root read first (not found → the root-loss mark), then the parent (not
        found → its document reset). Raises."""
        root_id = ctx.need_root.root_item_id
        self._take_read(ctx)
        try:
            await self._drive.get_change(ctx.handle, root_id)
        except DriveItemNotFoundError:
            await self._root_missing(ctx)
        if parent != root_id:
            self._take_read(ctx)
            try:
                await self._drive.get_change(ctx.handle, parent)
            except DriveItemNotFoundError:
                await self._reset_folder_item(ctx, parent)
                raise _ChainReset()
        raise ValueError("a create answered not found while its parent exists")

    async def _resolve_intent(self, ctx: "_Run", doc: MirrorDoc, size: Optional[int]) -> MirrorDoc:
        """An intent left by an earlier run (Δ210): read the name. Ours (created at or after `pending_at` minus the
        skew, a file of the size the intent uploaded — its `pending_size`, else `size` for an intent written before
        that field — and held by no other ledger document) → adopted, with no rendered hash so it is rewritten by id;
        foreign → the name is taken; absent → nothing there. The intent is cleared either way (a new create writes its
        own). The intent's size wins over the fresh render's (final review M1, Δ238): any byte change between the crash
        and the re-run (the UI language, the header link, a new transcript version) would otherwise judge the bot's own
        file foreign and leave it in the mirror for good."""
        self._take_read(ctx)
        found = await self._drive.child_by_name(ctx.handle, doc.pending_parent_id, doc.pending_name)
        cleared = replace(doc, pending_op="", pending_name="", pending_parent_id="", pending_at=None, pending_size=0)
        if found is None:
            return cleared
        folder = doc.kind != KIND_FILE
        want = doc.pending_size or size
        holder = await self._mirror.by_mirror_item(ctx.user_id, found.item_id)
        held_by_another = holder is not None and holder.key != doc.key
        if not held_by_another and _ours(found.is_folder, found.created_at, found.size_bytes, doc.pending_at, want,
                                         folder):
            adopted = replace(cleared, mirror_item_id=found.item_id, mirror_parent_id=doc.pending_parent_id,
                              mirror_name=found.name or doc.pending_name, mirror_ctag=found.content_version,
                              rendered_hash="")
            ctx.counters["mirror_adopted"] += 1
            return await self._saved(ctx, adopted)
        ctx.taken_in(doc.pending_parent_id).add(fold(doc.pending_name))
        ctx.counters["mirror_foreign"] += 1
        return cleared

    async def _intent(self, ctx: "_Run", doc: MirrorDoc, parent: str, name: str, size: int = 0) -> MirrorDoc:
        self._take_op(ctx)
        return await self._saved(ctx, replace(doc, pending_op="create", pending_name=name, pending_parent_id=parent,
                                              pending_size=size,
                                              pending_at=ctx.now))

    async def _create_folder(self, ctx: "_Run", doc: MirrorDoc, parent: str, name: str) -> MirrorDoc:
        await self._ensure_inside(ctx, parent)
        doc = await self._intent(ctx, doc, parent, name)
        try:
            item = await self._drive.create_folder(ctx.handle, parent, name)
        except DriveNameConflictError:
            raise _NameTaken(parent, name)
        except DriveItemNotFoundError:
            await self._create_404(ctx, parent)
            raise
        return await self._saved(ctx, replace(doc, mirror_item_id=item.item_id, mirror_parent_id=parent,
                                              mirror_name=name, mirror_ctag=item.content_version, pending_op="",
                                              pending_name="", pending_parent_id="", pending_at=None, pending_size=0))

    async def _upload(self, ctx: "_Run", work: "_FileWork", doc: MirrorDoc, parent: str, name: str) -> MirrorDoc:
        await self._ensure_inside(ctx, parent)
        doc = await self._intent(ctx, _snapshot(doc, work), parent, name, len(work.data))
        try:
            item = await self._drive.upload(ctx.handle, parent, name, work.data, MD_TYPE, conflict="fail")
        except DriveNameConflictError:
            raise _NameTaken(parent, name)
        except DriveItemNotFoundError:
            await self._create_404(ctx, parent)
            raise
        return await self._saved(ctx, replace(doc, mirror_item_id=item.item_id, mirror_parent_id=parent,
                                              mirror_name=name, mirror_ctag=item.content_version, rendered_hash=work.hash,
                                              render_version=plan.RENDER_VERSION, render_language=ctx.host.language,
                                              pending_op="", pending_name="", pending_parent_id="", pending_at=None,
                                              pending_size=0))

    async def _settle_after(self, ctx: "_Run", folder_id: str) -> None:
        """A name was freed in `folder_id` (a move away, an archive, a prune): its names are assigned again and the
        suffixed documents whose collision is gone renamed back, within the budget. Archive folders are frozen."""
        if not folder_id:
            return
        root_id = ctx.need_root.root_item_id
        level = LEVEL_ROOT
        if folder_id != root_id:
            folder = await self._doc_by_item(ctx, folder_id)
            if folder is None or folder.state != MIRROR_ACTIVE:
                return
            long_paths = archive_folder_key(ctx.user_id, ctx.drive_identity, "", LONG_PATHS_NAME)
            if folder.kind == KIND_ARCHIVE and folder.key != long_paths:
                return
            level = LEVEL_OTHER

        async def settle() -> None:
            for _ in range(NAME_RETRIES):
                _, moves, docs = await self._plan_folder(ctx, folder_id, level, None, None, frozen=False)
                try:
                    for move in moves:
                        docs[move.key] = await self._patch(ctx, docs[move.key], move, sibling=True)
                    return
                except _NameTaken as taken:
                    ctx.taken_in(taken.parent).add(fold(taken.name))
                    ctx.counters["mirror_foreign"] += 1
                except _SiblingStuck as stuck:
                    ctx.fixed.add(stuck.key)
                except _PatchNotFound as missing:
                    if not await self._patch_404(ctx, missing.doc, missing.parent):
                        raise ValueError("a rename answered not found while the item exists")
                    await self._sibling_gone(ctx, missing.doc)

        await self._guard(ctx, "settle", settle(), None)

    # -- step 5: prune -----------------------------------------------------------------------------------------------

    async def _prune(self, ctx: "_Run") -> None:
        for doc in ctx.prune:
            self._check_time(ctx)

            async def charge(doc: MirrorDoc = doc) -> None:
                new = charged_doc(doc, ctx.now)
                held_until = new.failed_at + plan.MIRROR_FAILED_RETRY if new.failed_at is not None else new.retry_at
                await self._save_doc(ctx, mark_held(new, held_until) if held_until is not None else new)

            await self._guard(ctx, "prune", self._prune_one(ctx, doc), charge)

    async def _prune_one(self, ctx: "_Run", doc: MirrorDoc) -> None:
        """One listing (a read): empty → the folder is deleted (to the recycle bin) with its document; a foreign item
        inside → kept and counted, listed again a week later; already gone → the document goes."""
        self._take_read(ctx)
        try:
            page = await self._drive.list_children_changes(ctx.handle, doc.mirror_item_id, 1)
        except DriveItemNotFoundError:
            ctx.host.fence()
            await self._mirror.delete_doc(ctx.user_id, doc.key)
            return
        if page.changes:
            ctx.counters["mirror_foreign"] += 1
            ctx.host.fence()
            await self._mirror.hold_mark(ctx.user_id, doc.key, ctx.now + FOREIGN_FOLDER_HOLD)
            return
        await self._ensure_inside(ctx, doc.mirror_item_id)
        self._take_op(ctx)
        try:
            await self._drive.delete(ctx.handle, doc.mirror_item_id)
        except DriveItemNotFoundError:
            logger.debug(f"[DriveMirror] user={ctx.user_id[:8]} a pruned folder was already gone")
        ctx.host.fence()
        await self._mirror.delete_doc(ctx.user_id, doc.key)
        ctx.counters["mirror_pruned"] += 1
        await self._settle_after(ctx, doc.mirror_parent_id)

    # -- step 6: the rotating scan (§27.9.9) -----------------------------------------------------------------------------

    async def _scan_page(self, ctx: "_Run") -> None:
        """MIRROR_SCAN_PAGE file documents of the drive from `scan_cursor`, only while the epoch is idle after a complete
        round (during an enumeration or a sweep records are missing by design). An active document whose record is
        missing and which has no mark gets `unknown` — whatever the record's digest was, so a file whose record was
        deleted before its digest was stamped (no deletion notice) is still found. A detached document not checked for
        MIRROR_AUDIT_DAYS (and not backing off) gets one `get_change` of its source. The marks of the page come first
        (store only), the reads after; the cursor moves only once the page is done, so a stopped page is read again."""
        if not (ctx.host.round_complete and ctx.host.epoch_idle):
            return
        page = await self._mirror.scan_page(ctx.user_id, ctx.drive_identity, ctx.need_root.scan_cursor or None,
                                            MIRROR_SCAN_PAGE)
        docs = [d for d in page if d.drive_identity == ctx.drive_identity and d.kind == KIND_FILE]
        recs = await self._records(ctx, [d.source_item_id for d in docs])
        lost = [d for d in docs if d.state == MIRROR_ACTIVE and not d.gone_pending and d.source_item_id not in recs]
        if lost:
            await self._guard(ctx, "scan marks", self._scan_marks(ctx, lost), None)
        stale = ctx.now - timedelta(days=MIRROR_AUDIT_DAYS)
        for doc in docs:
            if doc.state != MIRROR_DETACHED or doc.source_item_id in recs or doc.gone_pending:
                continue
            if doc.checked_at is not None and doc.checked_at > stale:
                continue
            if not retry_due(doc, now=ctx.now):
                continue
            self._check_time(ctx)
            await self._guard(ctx, f"recheck {_hid(doc.source_item_id)}", self._recheck(ctx, doc),
                              functools.partial(self._doc_charge, ctx, doc))
        cursor = page[-1].key if len(page) >= MIRROR_SCAN_PAGE else ""
        if cursor != ctx.need_root.scan_cursor:
            await self._save_root(ctx, replace(ctx.need_root, scan_cursor=cursor))

    async def _scan_marks(self, ctx: "_Run", docs: Sequence[MirrorDoc]) -> None:
        ctx.host.fence()
        marked_n = await self._mirror.mark_gone(
            ctx.user_id, [DeletionNotice(d.source_item_id, UNKNOWN, ctx.now, source_identity=d.source_identity)
                          for d in docs], drive_identity=ctx.drive_identity)
        ctx.counters["mirror_scan_marked"] += marked_n

    async def _recheck(self, ctx: "_Run", doc: MirrorDoc) -> None:
        """A detached document's weekly check (§27.9.6): one read of its source. Found → checked again in a week (a
        record that comes back reattaches it through step 4). Not found → a `gone` mark, which the mark step archives
        after the grace (Δ211: a 404 is read as gone for a source the index no longer holds)."""
        self._take_read(ctx)
        try:
            await self._drive.get_change(ctx.handle, doc.source_item_id)
            found = True
        except DriveItemNotFoundError:
            found = False
        fresh = await self._doc(ctx, doc.key)
        if fresh is None:
            return
        checked = replace(reset_charges(fresh), checked_at=ctx.now)
        if not found:
            checked = marked(checked, DeletionNotice(doc.source_item_id, GONE, ctx.now,
                                                     source_identity=doc.source_identity))
            ctx.counters["mirror_detached_gone"] += 1
        await self._save_doc(ctx, checked)
        ctx.counters["mirror_rechecked"] += 1

    async def _doc_charge(self, ctx: "_Run", doc: MirrorDoc, seen: bool = False) -> None:
        """A failed scan or audit action on a document with no due record: `attempts` + 1 and a backoff on the
        document (parked at MIRROR_MAX_ATTEMPTS). `seen` (the audit): the document also counts as seen this cycle, so
        a failing one never holds the walk; the next cycle tries it again. Another drive's document under the key is
        never written."""
        try:
            fresh = await self._doc(ctx, doc.key)
        except _OtherDrive:
            return
        if fresh is None:
            return
        new = charged_doc(fresh, ctx.now)
        if new.failed_at is not None and fresh.failed_at is None:
            ctx.counters["mirror_failed"] += 1
        await self._save_doc(ctx, replace(new, audit_seen_at=ctx.now) if seen else new)

    # -- step 7: the audit (§27.9.9, Δ201) -------------------------------------------------------------------------------

    async def _audit_page(self, ctx: "_Run") -> None:
        """At most every MIRROR_AUDIT_DAYS (`audit_due`): the walk goes on from `audit_folder_key` (""= the root, a
        folder or archive-folder document's key, AUDIT_AREA = the App Folder root) and `audit_page_cursor`, one listing
        page per read, until the budgets or the deadline stop it. The cycle ends after the last stage: `audited_at` and
        both cursors reset."""
        if not ctx.host.round_complete or not audit_due(ctx.need_root, ctx.now):
            return
        while True:
            self._check_time(ctx)
            stage = ctx.need_root.audit_folder_key
            if stage == AUDIT_AREA:
                finished = await self._audit_area(ctx)
            elif not stage:
                finished = await self._audit_folder(ctx, None)
            else:
                folder = await self._audit_stage_doc(ctx, stage)
                finished = True if folder is None else await self._audit_folder(ctx, folder)
            if not finished:
                continue
            nxt = await self._next_stage(ctx, stage)
            if nxt is None:
                await self._save_root(ctx, replace(ctx.need_root, audited_at=ctx.now, audit_folder_key="",
                                                   audit_page_cursor=""))
                ctx.counters["mirror_audited"] += 1
                logger.info(f"[DriveMirror] user={ctx.user_id[:8]} audit cycle done")
                return
            await self._save_root(ctx, replace(ctx.need_root, audit_folder_key=nxt, audit_page_cursor=""))

    async def _audit_stage_doc(self, ctx: "_Run", key: str) -> Optional[MirrorDoc]:
        """The folder document the walk is at; None (skipped) when it is gone, holds no item, or is another drive's."""
        try:
            doc = await self._doc(ctx, key)
        except _OtherDrive:
            ctx.counters["mirror_other_drive"] += 1
            return None
        if doc is None or not doc.mirror_item_id or doc.kind not in (KIND_FOLDER, KIND_ARCHIVE):
            return None
        return doc

    async def _next_stage(self, ctx: "_Run", stage: str) -> Optional[str]:
        """After the root: the first folder document by key; after a folder: the next one; after the last: the App
        Folder root, unless an orphan was reported already; after that: the end (None)."""
        if stage == AUDIT_AREA:
            return None
        nxt = await self._mirror.folder_docs(ctx.user_id, ctx.drive_identity, stage or None, 1)
        if nxt:
            return nxt[0].key
        return None if ctx.need_root.orphans_reported else AUDIT_AREA

    async def _audit_folder(self, ctx: "_Run", folder: Optional[MirrorDoc]) -> bool:
        """One listing page of the root (`folder` None) or of a folder document's item; True once its last page is
        done. A listing that fails is charged (a folder document) and the walk moves on. A 404: the root's sets the
        root-loss mark; a folder's treats every document beneath it as missing."""
        root = ctx.need_root
        folder_id = folder.mirror_item_id if folder is not None else root.root_item_id
        if root.audit_page_cursor == AUDIT_GONE and folder is not None:
            listed = await self._audit_folder_gone(ctx, folder)
            if listed is None:
                return True                                  # not confirmed: the folder waits for next cycle
        elif root.audit_page_cursor != AUDIT_CONFIRM:
            listed = await self._audit_listing(ctx, folder, folder_id)
            if listed is None:
                return True                                  # the listing failed: the folder waits for next cycle
            if not listed:
                return False
        threshold = root.audited_at or root.created_at
        for doc in await self._mirror.children(ctx.user_id, ctx.drive_identity, folder_id, CHILDREN_MAX):
            if doc.drive_identity != ctx.drive_identity or not doc.mirror_item_id or doc.key in ctx.audit_seen:
                continue
            if threshold is not None and doc.audit_seen_at is not None and doc.audit_seen_at > threshold:
                continue                                     # seen on an earlier page of this cycle
            self._check_time(ctx)
            ctx.audit_seen.add(doc.key)
            await self._guard(ctx, f"audit unseen {_kind(doc.key)} {_hid(doc.source_item_id)}",
                              self._audit_unseen(ctx, doc, folder_id),
                              functools.partial(self._doc_charge, ctx, doc, True))
        if folder is None:
            await self._guard(ctx, "audit readme", self._audit_readme(ctx), None)
        return True

    async def _audit_listing(self, ctx: "_Run", folder: Optional[MirrorDoc], folder_id: str) -> Optional[bool]:
        """One listing page of the folder, compared with its documents. False when more pages follow (the next link is
        saved); True once the last page is done (the cursor becomes AUDIT_CONFIRM, so a walk stopped while confirming
        resumes without listing again) or once a 404 was confirmed and handled; None when the listing failed
        (charged) or a 404 was not confirmed, so its documents are neither read one by one on a guess nor discarded.
        A 404 first saves AUDIT_GONE: a walk stopped before the confirming read spends its next read on that read,
        not on listing again, so one read a tick still makes progress in every state."""
        root = ctx.need_root
        label = f"audit {'root' if folder is None else _kind(folder.key)} listing"
        box: List[Optional[Any]] = []

        async def listing() -> None:
            self._take_read(ctx)
            try:
                box.append(await self._drive.list_children_changes(ctx.handle, folder_id, AUDIT_PAGE,
                                                                   root.audit_page_cursor or None))
            except DriveItemNotFoundError:
                box.append(None)

        await self._guard(ctx, label, listing(),
                          functools.partial(self._doc_charge, ctx, folder, True) if folder is not None else None)
        if not box:
            return None
        page = box[0]
        if page is None:
            if folder is None:
                await self._root_missing(ctx)          # the root answered not found: the mark, and the step ends
                return True
            await self._save_root(ctx, replace(ctx.need_root, audit_page_cursor=AUDIT_GONE))
            return await self._audit_folder_gone(ctx, folder)
        kids = await self._mirror.children(ctx.user_id, ctx.drive_identity, folder_id, CHILDREN_MAX)
        by_item = {k.mirror_item_id: k for k in kids if k.drive_identity == ctx.drive_identity and k.mirror_item_id}
        for change in page.changes:
            self._check_time(ctx)
            if folder is None and root.readme_item_id and change.item_id == root.readme_item_id:
                ctx.readme_listed = change
                continue
            doc = by_item.get(change.item_id) or await self._doc_by_item(ctx, change.item_id)
            if doc is None:
                ctx.counters["mirror_foreign"] += 1          # not ours: left alone, never deleted, never indexed
                continue
            ctx.audit_seen.add(doc.key)
            await self._guard(ctx, f"audit {_kind(doc.key)} {_hid(doc.source_item_id)}",
                              self._audit_listed(ctx, doc, change, folder_id),
                              functools.partial(self._doc_charge, ctx, doc, True))
        await self._save_root(ctx, replace(ctx.need_root, audit_page_cursor=page.next_cursor or AUDIT_CONFIRM))
        return page.next_cursor is None

    async def _audit_folder_gone(self, ctx: "_Run", folder: MirrorDoc) -> Optional[bool]:
        """A folder whose listing answered 404 (the cursor is AUDIT_GONE). A discard is irreversible (archived documents
        deleted, pins released), so one confirming read comes first and only a not-found answer acts: everything
        beneath it is treated as missing, and the cursor becomes AUDIT_CONFIRM (True). Found, or a failed read: None,
        the folder waits for the next cycle."""
        found = await self._item_found(ctx, folder.mirror_item_id, "folder", "the folder waits for the next cycle")
        if found is not False:
            if found:
                ctx.counters["mirror_folder_404_unconfirmed"] += 1
            return None
        ctx.counters["mirror_audit_folder_gone"] += 1
        await self._guard(ctx, f"audit {_kind(folder.key)} gone", self._discard_beneath(ctx, folder),
                          functools.partial(self._doc_charge, ctx, folder, True))
        await self._save_root(ctx, replace(ctx.need_root, audit_page_cursor=AUDIT_CONFIRM))
        return True

    def _frozen(self, ctx: "_Run", doc: MirrorDoc) -> bool:
        """The archive's documents (archived files, the archive folders; the root's `Long paths` holds active files and
        is not one) and detached files: a move out of the mirror ends them (`_audit_unseen`). Inside the mirror an
        archive folder's edit or move is followed; an archived or detached FILE is rewritten and moved back instead
        (`_audit_archived`, Δ241, Δ243)."""
        if doc.state in (MIRROR_ARCHIVED, MIRROR_DETACHED):
            return True
        return doc.kind == KIND_ARCHIVE and doc.key != archive_folder_key(ctx.user_id, ctx.drive_identity, "",
                                                                          LONG_PATHS_NAME)

    async def _audit_listed(self, ctx: "_Run", doc: MirrorDoc, change: DriveChange, folder_id: str) -> None:
        """A document's item listed in `folder_id`. Frozen: the document follows a rename, a move or an edit, nothing
        is written to the drive. Active tree: an item not where the ledger has it is moved back; a file whose `cTag`
        differs is made due with no rendered hash, so step 4 rewrites it from the store. The sighting is stamped."""
        here = (doc.mirror_parent_id, doc.mirror_name) == (folder_id, change.name)
        if doc.state in (MIRROR_ARCHIVED, MIRROR_DETACHED) and doc.kind == KIND_FILE:
            await self._audit_archived(ctx, doc, change, here)
            return
        if self._frozen(ctx, doc):
            new = doc
            if not here:
                new = replace(new, mirror_parent_id=folder_id, mirror_name=change.name)
                ctx.counters["mirror_followed"] += 1
            if doc.kind == KIND_FILE and change.content_version and change.content_version != doc.mirror_ctag:
                if doc.mirror_ctag:
                    ctx.counters["mirror_edit_left"] += 1
                new = replace(new, mirror_ctag=change.content_version)
            await self._save_doc(ctx, replace(new, audit_seen_at=ctx.now))
            return
        if not here:
            await self._move_back(ctx, doc, change)
            return
        if doc.kind == KIND_FILE and change.content_version and change.content_version != doc.mirror_ctag:
            if doc.mirror_ctag:
                await self._save_doc(ctx, replace(doc, rendered_hash="", audit_seen_at=ctx.now))
                await self._make_due(ctx, doc)
                ctx.counters["mirror_rewrite_due"] += 1
            else:                                            # no cTag recorded: this one becomes the reference
                await self._save_doc(ctx, replace(doc, mirror_ctag=change.content_version, audit_seen_at=ctx.now))
            return
        await self._save_doc(ctx, replace(doc, audit_seen_at=ctx.now))

    async def _audit_archived(self, ctx: "_Run", doc: MirrorDoc, change: DriveChange, here: bool) -> None:
        """An archived or a detached file listed inside the mirror (owner ruling 2026-10-21, Δ241: every `.md` in
        `_Transcripts`, active or archived, is the bot's and is overwritten; a version of one's own is a copy in another
        folder; a detached file keeps its pinned store entry too, Δ190, so it is treated the same, Δ243). An edit (its
        `cTag` differs from the one recorded) is rewritten from the pinned store entry first (`_rewrite_archived`); a
        rename or a move inside the mirror is then moved back to its ledger place, like an active file. With no `cTag`
        recorded the one listed becomes the reference. A deletion, or a move out of the mirror, keeps its own handling
        (`_doc_gone`: an archived document deleted and unpinned, a detached one keeps its pin), and nothing is ever
        re-created."""
        edited = bool(doc.mirror_ctag) and bool(change.content_version) and change.content_version != doc.mirror_ctag
        if edited:
            doc = await self._rewrite_archived(ctx, doc, change)
        elif change.content_version and not doc.mirror_ctag:
            doc = replace(doc, mirror_ctag=change.content_version)
        if not here:
            await self._move_back(ctx, doc, change)
            return
        await self._save_doc(ctx, replace(doc, audit_seen_at=ctx.now))

    async def _rewrite_archived(self, ctx: "_Run", doc: MirrorDoc, change: DriveChange) -> MirrorDoc:
        """One `replace_content` of the bot's own archived item, by id, with `render_archived` of its pinned store entry
        (in the current UI language), after the item and its archive folder are checked inside the root. No store entry
        and no stub: nothing to rewrite from, the edit is left (`mirror_edit_left`) and its `cTag` becomes the
        reference (saved by the caller with the sighting), so the next cycle does not try again. Returns the document."""
        stored = await self._repo.get_transcript(ctx.user_id, doc.content_key) if doc.content_key else None
        if stored is None and doc.transcript_stub:
            stored = StoredTranscript(doc.content_key, None, stub=doc.transcript_stub)
        if stored is None:
            ctx.counters["mirror_edit_left"] += 1
            return replace(doc, mirror_ctag=change.content_version)
        data = render_archived(stored, doc, ctx.host.strings())
        await self._ensure_inside(ctx, doc.mirror_item_id)
        await self._ensure_inside(ctx, doc.mirror_parent_id)
        self._take_op(ctx)
        item = await self._drive.replace_content(ctx.handle, doc.mirror_item_id, data, MD_TYPE)
        ctx.counters["mirror_archive_rewritten"] += 1
        return await self._saved(ctx, replace(doc, mirror_ctag=item.content_version, rendered_hash=rendered_hash(data),
                                              render_language=ctx.host.language, audit_seen_at=ctx.now))

    async def _move_back(self, ctx: "_Run", doc: MirrorDoc, change: DriveChange) -> bool:
        """An item of the active tree moved or renamed by hand goes back where the ledger has it (one PATCH of an item
        the bot created, to a destination inside the root). A taken name or a vanished destination: the document
        follows the item and its record is made due, so step 4 places it with the name rules (yields, suffixes,
        chains). A document with a pending gone mark (a prune waiting) is not re-pointed: its ledger place stays and it is
        charged (`charged_doc`: back-off, then parked for MIRROR_FAILED_RETRY, the mark held to match), since no record
        would place it again. The recorded `cTag` is kept (a move never changes content); an ACTIVE file whose listed
        `cTag` differs from it was edited as well: its rendered hash is cleared and its record made due on every path
        (back, blocked, or already back), so step 4 rewrites it in the same cycle (Δ242). True when the item is back."""
        if not doc.mirror_parent_id:
            return False
        edited = (doc.state == MIRROR_ACTIVE and doc.kind == KIND_FILE and bool(doc.mirror_ctag)
                  and bool(change.content_version) and change.content_version != doc.mirror_ctag)
        redo = {"rendered_hash": ""} if edited else {}
        await self._ensure_inside(ctx, doc.mirror_parent_id)
        new_parent = doc.mirror_parent_id if change.parent_id != doc.mirror_parent_id else None
        new_name = doc.mirror_name if change.name != doc.mirror_name else None
        if new_parent is None and new_name is None:
            # `_ensure_inside` above already moved an ancestor of ours back: this item sits where the ledger has it,
            # and no PATCH is sent, so no op is spent
            if doc.kind != KIND_FILE:
                ctx.inside[doc.mirror_item_id] = True
            if edited:
                await self._save_doc(ctx, replace(doc, audit_seen_at=ctx.now, **redo))
                await self._make_due(ctx, doc)
                ctx.counters["mirror_rewrite_due"] += 1
            return True
        self._take_op(ctx)
        try:
            item = await self._drive.move(ctx.handle, doc.mirror_item_id, new_parent_id=new_parent,
                                          new_name=new_name)
        except (DriveNameConflictError, DriveItemNotFoundError):
            ctx.counters["mirror_move_back_blocked"] += 1
            if doc.gone_pending:
                # a document waiting for its mark or prune has no record to place it again: the ledger keeps where it
                # belongs and the document is charged like `_prune`'s own failure: back-off, then parked for
                # MIRROR_FAILED_RETRY. The mark is held to the same time, so it is not ready (and takes no reserve)
                # while backed off or parked
                new = charged_doc(replace(doc, audit_seen_at=ctx.now), ctx.now)
                if new.failed_at is not None and doc.failed_at is None:
                    ctx.counters["mirror_failed"] += 1
                held_until = new.failed_at + plan.MIRROR_FAILED_RETRY if new.failed_at is not None else new.retry_at
                await self._save_doc(ctx, mark_held(new, held_until) if held_until is not None else new)
                return False
            await self._save_doc(ctx, replace(doc, mirror_parent_id=change.parent_id, mirror_name=change.name,
                                              audit_seen_at=ctx.now, **redo))
            await self._make_due(ctx, doc)
            if edited:
                ctx.counters["mirror_rewrite_due"] += 1
            return False
        await self._save_doc(ctx, replace(doc, mirror_ctag=doc.mirror_ctag or item.content_version, audit_seen_at=ctx.now, **redo))
        ctx.counters["mirror_moved_back"] += 1
        if edited:
            await self._make_due(ctx, doc)
            ctx.counters["mirror_rewrite_due"] += 1
        if doc.kind != KIND_FILE:
            ctx.inside[doc.mirror_item_id] = True
        return True

    async def _make_due(self, ctx: "_Run", doc: MirrorDoc) -> None:
        """A file's or a source folder's record made due again (`due_again`), so step 4 places or writes its item; a
        record that would not be due (gone, not eligible, held) is left as it is."""
        if doc.kind not in (KIND_FILE, KIND_FOLDER):
            return
        rec = await self._record(ctx, doc.source_item_id)
        if rec is not None and due_again(rec).mirror_due:
            await self._commit(ctx, [due_again(rec)], done=False)

    async def _audit_unseen(self, ctx: "_Run", doc: MirrorDoc, folder_id: str) -> None:
        """A document of the folder whose item no listing showed this cycle: one read of the item. Not found: missing.
        Found in the folder after all: as listed. Elsewhere: an active item is moved back; a frozen one follows a move
        inside the mirror, and a move out of it ends the document like a deletion."""
        self._take_read(ctx)
        try:
            change = await self._drive.get_change(ctx.handle, doc.mirror_item_id)
        except DriveItemNotFoundError:
            await self._audit_gone(ctx, doc)
            return
        if change.parent_id == folder_id:
            await self._audit_listed(ctx, doc, change, folder_id)
        elif not self._frozen(ctx, doc):
            await self._move_back(ctx, doc, change)
        elif await self._inside(ctx, change.parent_id):
            await self._audit_listed(ctx, doc, change, change.parent_id)
        else:
            ctx.counters["mirror_moved_out"] += 1
            await self._audit_gone(ctx, doc)

    async def _inside(self, ctx: "_Run", parent_id: str) -> bool:
        """Is `parent_id` the root or below it? Folders of the ledger are walked by their documents, any other folder by
        a read; the App Folder root, a not-found answer or a cycle means outside (a previous root is outside too)."""
        root_id = ctx.need_root.root_item_id
        area = await self._area_root_id(ctx)
        seen: Set[str] = set()
        while parent_id and parent_id not in seen and len(seen) < MAX_CHAIN_DEPTH:
            if parent_id == root_id:
                return True
            if parent_id == area:
                return False
            seen.add(parent_id)
            folder = await self._doc_by_item(ctx, parent_id)
            if folder is not None and folder.mirror_parent_id:
                parent_id = folder.mirror_parent_id
                continue
            self._take_read(ctx)
            try:
                parent_id = (await self._drive.get_change(ctx.handle, parent_id)).parent_id
            except DriveItemNotFoundError:
                return False
        return False

    async def _audit_gone(self, ctx: "_Run", doc: MirrorDoc) -> None:
        """A document's item is missing, or out of the mirror: a folder's documents beneath first, then its own."""
        if doc.kind in (KIND_FOLDER, KIND_ARCHIVE):
            await self._discard_beneath(ctx, doc)
        else:
            await self._doc_gone(ctx, doc)

    async def _discard_beneath(self, ctx: "_Run", folder: MirrorDoc, depth: int = 0) -> None:
        """A folder that answers 404 (or a frozen one moved out of the mirror): every document beneath it, through
        `children` and recursively, is treated as missing, then the folder's own document. Each handled document leaves
        `children` (deleted, or no longer placed), so a stopped discard resumes; the folder goes last."""
        if depth > MAX_CHAIN_DEPTH:
            raise _Unplaceable("mirror folders nested too deep")
        for _ in range(MAX_CHAIN_DEPTH * 64):
            kids = [k for k in await self._mirror.children(ctx.user_id, ctx.drive_identity, folder.mirror_item_id,
                                                           CHILDREN_MAX) if k.drive_identity == ctx.drive_identity]
            for kid in kids:
                self._check_time(ctx)
                if kid.kind in (KIND_FOLDER, KIND_ARCHIVE) and kid.mirror_item_id:
                    await self._discard_beneath(ctx, kid, depth + 1)
                else:
                    await self._doc_gone(ctx, kid)
            if len(kids) < CHILDREN_MAX:
                break
        await self._doc_gone(ctx, folder)

    async def _doc_gone(self, ctx: "_Run", doc: MirrorDoc) -> None:
        """One document whose item is not in the mirror (§27.9.9). Archived file: the owner deleted it (or took it out
        of the mirror): the document is deleted, its pin released, never re-created. Archive folder: deleted, created
        again on demand. Active file: no item, its record due, so it is created again from the store. Detached file
        and folder documents: no item (a detached one keeps its pin)."""
        if doc.state == MIRROR_ARCHIVED or doc.kind == KIND_ARCHIVE:
            ctx.host.fence()
            await self._mirror.delete_doc(ctx.user_id, doc.key)
            ctx.counters["mirror_archive_owner_deleted" if doc.kind == KIND_FILE else "mirror_archive_folder_gone"] += 1
            return
        await self._save_doc(ctx, cleared_item(doc))
        if doc.state == MIRROR_ACTIVE and doc.kind == KIND_FILE:
            await self._make_due(ctx, doc)
            ctx.counters["mirror_recreate_due"] += 1

    async def _audit_readme(self, ctx: "_Run") -> None:
        """`_README.md` (§27.9.1): its `cTag` differs from `readme_ctag` → rewritten by id (in the current UI
        language); missing, renamed or moved away → forgotten (the moved file stays the owner's) and written again by
        step 1's rule, which never overwrites a name."""
        root = ctx.need_root
        if not root.readme_item_id:
            return
        change: Optional[DriveChange] = ctx.readme_listed
        if change is None:
            self._take_read(ctx)
            try:
                change = await self._drive.get_change(ctx.handle, root.readme_item_id)
            except DriveItemNotFoundError:
                change = None
        if change is None or change.parent_id != root.root_item_id or fold(change.name) != fold(README_NAME):
            ctx.counters["mirror_readme_missing"] += 1
            await self._save_root(ctx, replace(root, readme_item_id="", readme_ctag=""))
            await self._readme(ctx)
            return
        if change.content_version == root.readme_ctag:
            return
        if not root.readme_ctag:
            await self._save_root(ctx, replace(root, readme_ctag=change.content_version))
            return
        self._take_op(ctx)
        item = await self._drive.replace_content(ctx.handle, root.readme_item_id, readme_bytes(ctx.host.strings()),
                                                 MD_TYPE)
        await self._save_root(ctx, replace(ctx.need_root, readme_ctag=item.content_version))
        ctx.counters["mirror_readme_written"] += 1

    async def _audit_area(self, ctx: "_Run") -> bool:
        """The App Folder root, one page per read, only until something was reported (`orphans_reported`): a folder
        named like the root (`is_root_name`) that is no root of any of the user's drives (the crash orphan of §27.9.2),
        and a previous root that is present again, are reported once (one WARNING, counters); never adopted, never
        written. True when the stage is done."""
        root = ctx.need_root
        if root.orphans_reported:
            return True
        area = await self._area_root_id(ctx)
        box: List[Any] = []

        async def listing() -> None:
            self._take_read(ctx)
            box.append(await self._drive.list_children_changes(ctx.handle, area, AUDIT_PAGE,
                                                               root.audit_page_cursor or None))

        await self._guard(ctx, "audit app folder listing", listing(), None)
        if not box:
            return True
        page = box[0]
        roots = await self._mirror.roots(ctx.user_id)
        current = {r.root_item_id for r in roots if r.root_item_id} | {root.root_item_id}
        previous = {p for r in roots for p in r.previous_root_ids if p} - current
        orphans = sum(1 for c in page.changes if c.is_folder and c.item_id not in current
                      and c.item_id not in previous and is_root_name(c.name))
        restored = sum(1 for c in page.changes if c.item_id in previous)
        if orphans or restored:
            ctx.counters["mirror_orphan_roots"] += orphans
            ctx.counters["mirror_previous_roots_present"] += restored
            logger.warning(f"[DriveMirror] user={ctx.user_id[:8]} the App Folder holds {orphans} folder(s) named like "
                           f"the transcript root that are no root, and {restored} previous root(s): reported once, "
                           f"never adopted or written")
            await self._save_root(ctx, replace(ctx.need_root, orphans_reported=True))
            return True
        if page.next_cursor:
            await self._save_root(ctx, replace(ctx.need_root, audit_page_cursor=page.next_cursor))
            return False
        return True


# =====================================================================================================================
# helpers
# =====================================================================================================================

class _StopStep(Exception):
    """Ends the step for this tick: a budget or the deadline is spent, the drive is full, or the root is missing."""


class _Unplaceable(ValueError):
    """The source's or the mirror's folder chain cannot be resolved: charged like a refused write."""


class _OtherDrive(Exception):
    """A ledger document read by key belongs to another drive (the keys carry no drive id, OneDrive ids are
    drive-unique): the action is skipped and nothing is written under that key."""


class _Outside(Exception):
    """A write's folder is not inside the root (RFC §27.12: the mirror never writes outside it): nothing written, the
    item not charged; the folder of ours that left the root was read and, when it really is outside, moved back."""


class _ChainReset(Exception):
    """A destination folder was not found and its document reset: tried again next tick, nothing charged."""


class _SiblingStuck(Exception):
    """A sibling's rename answered path-too-long: it keeps its name for the rest of the run."""

    def __init__(self, key: str) -> None:
        super().__init__("sibling stuck")
        self.key = key


class _NameTaken(Exception):
    """A create or a move answered 409: a foreign item holds `name` in `parent`."""

    def __init__(self, parent: str, name: str) -> None:
        super().__init__("name taken")
        self.parent = parent
        self.name = name


class _PatchNotFound(Exception):
    """A PATCH answered 404: the item or its destination."""

    def __init__(self, doc: MirrorDoc, parent: str) -> None:
        super().__init__("patch not found")
        self.doc = doc
        self.parent = parent


@dataclass
class _Run:
    """One run's state: the root, the lazily read paths, the records read, the names foreign items hold, the records
    written, the folders to prune, and what the audit saw."""
    user_id: str
    handle: DriveHandle   # the connection the tick is for: every drive call and record read (MULTI_DRIVE_RFC §8)
    host: MirrorHost
    counters: Counter
    root: Optional[MirrorRoot] = None
    root_created: bool = False
    area_root_id: Optional[str] = None
    area_path: Optional[str] = None
    root_path: Optional[str] = None
    recs: Dict[str, Optional[DriveIndexRecord]] = field(default_factory=dict)
    taken: Dict[str, Set[str]] = field(default_factory=dict)
    fixed: Set[str] = field(default_factory=set)
    done: Set[str] = field(default_factory=set)
    prune: List[MirrorDoc] = field(default_factory=list)
    audit_seen: Set[str] = field(default_factory=set)          # documents the audit saw (listed or read) this run
    readme_listed: Optional[DriveChange] = None                # the read-me as the root's listing showed it this run
    inside: Dict[str, bool] = field(default_factory=dict)      # folder item id -> inside the root (this run's verdicts)
    repairing: Set[str] = field(default_factory=set)           # folders of ours read and repaired this run

    @property
    def stops(self) -> Tuple[Type[BaseException], ...]:
        '''The host's own stop signals: re-raised unchanged wherever a systemic class is.'''
        return tuple(self.host.stop_exceptions)

    @property
    def drive_identity(self) -> str:
        return self.host.drive_identity

    @property
    def now(self) -> datetime:
        return self.host.now()

    @property
    def need_root(self) -> MirrorRoot:
        if self.root is None:
            raise RuntimeError("the mirror root is not set")
        return self.root

    def taken_in(self, parent: str) -> Set[str]:
        return self.taken.setdefault(parent, set())


@dataclass
class _FileWork:
    rec: DriveIndexRecord
    chain: List[DriveIndexRecord]
    source_path: str
    data: bytes
    hash: str
    wrote: bool = False
    freed: List[str] = field(default_factory=list)


def _hid(item_id: str) -> str:
    """A short digest of an item id for log lines: an id's first characters are the drive's on a personal OneDrive."""
    return hashlib.sha256(item_id.encode("utf-8")).hexdigest()[:10]


def _log_unexpected(user_id: str, label: str, exc: BaseException, outcome: str = "charged") -> None:
    """ERROR with the type, the frames (code lines, no values) and a digest of the message to tell repeats apart: an
    exception's message, or its chained context, can carry a drive path or a file name (an adapter's not-found text),
    which no log line may (§27.12)."""
    digest = hashlib.sha256(str(exc).encode("utf-8")).hexdigest()[:12]
    frames = "".join(traceback.format_tb(exc.__traceback__))
    logger.error(f"[DriveMirror] user={user_id[:8]} {label}: unexpected {type(exc).__name__} (message digest {digest}), "
                 f"{outcome}\n{frames}")


def _children_wait(kids: Sequence[MirrorDoc], now: datetime) -> datetime:
    """When a gone folder's mark, held for the documents beneath it, is looked at again (Δ240): the earliest time one of
    them can move — its own mark's ripeness (`mark_ready_at`, which a back-off, a park or a foreign-item hold also sets;
    else `gone_at` plus the grace) — and never sooner than ANCESTOR_HOLD. A child with no mark of its own (step 4 is
    placing it elsewhere) keeps the short hold; detached children never move and do not count."""
    floor = now + ANCESTOR_HOLD
    times: List[datetime] = []
    for kid in kids:
        if kid.state == MIRROR_DETACHED:
            continue
        ready = kid.mark_ready_at
        if ready is None and kid.gone_at is not None:
            ready = kid.gone_at + MIRROR_ARCHIVE_GRACE
        if not kid.gone_pending or ready is None:
            return floor
        times.append(ready)
    return max(floor, min(times)) if times else floor


def _archive_name(doc: MirrorDoc) -> str:
    """The name a file is archived under: its drive name, unless that is a cycle's temporary name (a stopped swap),
    then its source's mirror name."""
    return mirror_file_name(doc.source_name) if _TMP.search(doc.mirror_name) else doc.mirror_name


def _kind(key: str) -> str:
    parts = key.split(":")
    return parts[-2] if len(parts) >= 3 else "doc"


def _ours(is_folder: bool, created_at: Optional[datetime], size: int, pending_at: Optional[datetime],
          want_size: Optional[int], folder: bool) -> bool:
    """An item found at an intent's name is the bot's own (Δ210, Δ217): the same kind, created at or after the intent
    minus MIRROR_CLOCK_SKEW, and for a file exactly the rendered size."""
    if is_folder != folder or created_at is None or pending_at is None:
        return False
    if created_at < pending_at - MIRROR_CLOCK_SKEW:
        return False
    return folder or (want_size is not None and size == want_size)


def _ledger_key(ctx: _Run, kind: str, rec: DriveIndexRecord) -> str:
    """The ledger key of the record's file or folder document on this mirror: by the mirror's drive identity and the
    record's item identity (MULTI_DRIVE_RFC §10; ValueError for a record without an identity)."""
    return ledger_key(ctx.user_id, kind, ctx.drive_identity, rec.identity)


def _file_doc(ctx: _Run, rec: DriveIndexRecord) -> MirrorDoc:
    return MirrorDoc(user_id=ctx.user_id, drive_identity=ctx.drive_identity, kind=KIND_FILE, source_item_id=rec.item_id,
                     source_identity=rec.identity, source_name=rec.name, source_parent_id=rec.parent_id,
                     content_key=rec.transcript_key)


def _folder_doc(ctx: _Run, rec: DriveIndexRecord) -> MirrorDoc:
    return MirrorDoc(user_id=ctx.user_id, drive_identity=ctx.drive_identity, kind=KIND_FOLDER, source_item_id=rec.item_id,
                     source_identity=rec.identity, source_name=rec.name, source_parent_id=rec.parent_id)


def _archive_doc(ctx: _Run, key: str, name: str) -> MirrorDoc:
    return MirrorDoc(user_id=ctx.user_id, drive_identity=ctx.drive_identity, kind=KIND_ARCHIVE,
                     source_item_id=key.rsplit(":", 1)[1], source_name=name)


def _reset_folder(doc: MirrorDoc) -> MirrorDoc:
    return replace(doc, mirror_item_id="", mirror_parent_id="", mirror_name="", mirror_ctag="", pending_op="",
                   pending_name="", pending_parent_id="", pending_at=None, pending_size=0)


def _snapshot(doc: MirrorDoc, work: _FileWork) -> MirrorDoc:
    """The fields an archive needs without the record (§27.9.3), refreshed at every render."""
    rec = work.rec
    return replace(doc, content_key=rec.transcript_key, source_path=work.source_path,
                   seconds=float(rec.transcript_seconds), speakers=rec.transcript_speakers,
                   languages=tuple(rec.transcript_languages), recorded_on=rec.effective_day,
                   transcript_stub=rec.transcript_stub)


def _folder_placed(doc: MirrorDoc, folder: DriveIndexRecord, parent: str) -> bool:
    """A folder document is where its record says: under `parent`, for the record's name and parent, at that name or
    its collision-suffixed form (a temporary name left by a stopped tick is not placed)."""
    if doc.mirror_parent_id != parent or doc.source_name != folder.name or doc.source_parent_id != folder.parent_id:
        return False
    return doc.mirror_name == folder.name or (doc.mirror_name.startswith(folder.name)
                                              and bool(_SUFFIX.fullmatch(doc.mirror_name[len(folder.name):])))


def _record_wanted(doc: MirrorDoc, rec: DriveIndexRecord) -> str:
    return mirror_file_name(rec.name) if doc.kind == KIND_FILE else rec.name


def _wanted(doc: MirrorDoc, rec: Optional[DriveIndexRecord]) -> str:
    """A sibling's wanted name: its record's, when the record is mirror-eligible in the same folder and the document
    was not re-placed in `Long paths` (so a swap of two names is one cycle, Δ192); else the document's source name."""
    if (rec is not None and rec.parent_id == doc.source_parent_id and not doc.long_path_source
            and (mirror_eligible(rec) if doc.kind == KIND_FILE else rec.state is not RecordState.DELETING)):
        return _record_wanted(doc, rec)
    return mirror_file_name(doc.source_name) if doc.kind == KIND_FILE else doc.source_name

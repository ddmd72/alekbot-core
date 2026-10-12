"""
Drive index planner (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §3.6, §5.2, §7). Pure: every decision the
tick makes lives here and is tested without Firestore, the drive provider or a clock. The service fetches, calls
these functions and writes what they return.

Public interface:
    PlanContext, Classification, ObservationPlan, AccessAction
    parent_scope(parent_id, parent, root_item_id) -> Optional[Scope]
    needs_probe(change, pscope, previous) -> bool
    classify(change, pscope, probe, previous) -> Classification
    plan_observation(change, existing, parent, probe, ctx, previous_parent=None, *, observed=True) -> ObservationPlan
    change_from_record(rec) -> DriveChange
    schedule(rec, now, *, first_seen) -> DriveIndexRecord
    with_card(rec, card_hash, embedding_space, now) / without_card(rec, now) / capped(rec, now)
    with_retry(rec, now) -> DriveIndexRecord
    access_action(enabled, state, connection, now) -> AccessAction
    is_stale(state, oldest_edit_due_at, now) -> bool
    stale_versions(rec) -> bool
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, List, Optional, Tuple

from .drive_card import card_eligible, card_hash, content_hash_of, effective_date, photo_location
from .drive_index import (
    CARD_VERSION, GEO_TABLE_VERSION, POLICY_VERSION, ContentClass, DriveChange, DriveIndexRecord,
    DriveSyncState, EpochPhase, FolderProbe, Priority, Reason, RecordState, Scope,
)
from .drive_index_params import ACCESS_LOSS_TTL, DENSE_FOLDER_CHILDREN, RETRY_BASE, RETRY_CAP, STALE_ALERT_AFTER
from .drive_index_policy import (
    ADMITTED_KINDS, card_rank_of, content_class_of, density_still_valid, is_dense, is_dot_name,
    is_generated_dir, is_marker_child, item_rule, kind_of,
)
from .drive_name_keys import name_keys
from .user_drive import DriveConnection

PlaceOf = Callable[[float, float], Optional[str]]


@dataclass(frozen=True)
class PlanContext:
    user_id: str
    root_item_id: str
    epoch: int
    phase: EpochPhase
    now: datetime
    place_of: PlaceOf


@dataclass(frozen=True)
class Classification:
    stored: bool                 # False: a file inside an opaque subtree gets no record (§5.2)
    scope: Scope
    state: RecordState
    reason: Optional[Reason]
    kind: str
    content_class: ContentClass
    probe_pending: bool = False
    density_count: int = 0       # childCount the density decision was taken at (0: none taken)
    density_reset: bool = False  # decided by size alone: an earlier density anchor no longer stands (retro F3)


@dataclass(frozen=True)
class ObservationPlan:
    upsert: Optional[DriveIndexRecord] = None
    delete_item_id: Optional[str] = None
    parent_updates: Tuple[DriveIndexRecord, ...] = ()   # the old and/or the new parent of a marker child


EMPTY = ObservationPlan()


class AccessAction(str, Enum):
    NOOP_DISABLED = "noop_disabled"            # indexing off (or access lost) and nothing stored
    PURGE_OPT_OUT = "purge_opt_out"            # indexing off, something stored
    RECORD_LOSS = "record_loss"                # access lost within the grace period: stamp, stop
    PURGE_EXPIRED = "purge_expired"            # access lost past ACCESS_LOSS_TTL
    PURGE_DRIVE_SWITCH = "purge_drive_switch"  # a different drive is connected
    RESUME_AFTER_REGAIN = "resume_after_regain"
    PURGE_THEN_RESTART = "purge_then_restart"  # regained after the TTL: finish the purge, start epoch 1
    PROCEED = "proceed"


def parent_scope(parent_id: str, parent: Optional[DriveIndexRecord], root_item_id: str) -> Optional[Scope]:
    """Scope a child inherits; None when the parent is not known yet. The root is an implicit normal parent."""
    if parent_id and parent_id == root_item_id:
        return Scope.NORMAL
    return parent.scope if parent is not None else None


def _density_decision_reusable(change: DriveChange, previous: Optional[DriveIndexRecord]) -> bool:
    return (previous is not None and previous.probed_child_count > 0
            and previous.policy_version == POLICY_VERSION
            and previous.scope in (Scope.NORMAL, Scope.OPAQUE_ROOT)
            and density_still_valid(previous.probed_child_count, change.child_count))


def needs_probe(change: DriveChange, pscope: Optional[Scope], previous: Optional[DriveIndexRecord]) -> bool:
    """A folder over the density threshold whose decision is not taken yet, or no longer stands (§3.6)."""
    return (change.is_folder and not change.deleted and pscope is Scope.NORMAL
            and not is_dot_name(change.name) and change.child_count > DENSE_FOLDER_CHILDREN
            and not _marker_opaque(previous)
            and not _density_decision_reusable(change, previous))


def _marker_opaque(rec: Optional[DriveIndexRecord]) -> bool:
    return (rec is not None and rec.scope is Scope.OPAQUE_ROOT and rec.reason is Reason.OPAQUE_ROOT
            and rec.policy_version == POLICY_VERSION and not is_generated_dir(rec.name))


def classify(change: DriveChange, pscope: Optional[Scope], probe: Optional[FolderProbe],
             previous: Optional[DriveIndexRecord]) -> Classification:
    kind = kind_of(change.name, change.mime_type, change.is_folder)
    cclass = content_class_of(kind)

    def c(scope: Scope, state: RecordState, reason: Optional[Reason], **kw: Any) -> Classification:
        return Classification(True, scope, state, reason, kind, cclass, **kw)

    if pscope is None or pscope is Scope.UNRESOLVED:
        return c(Scope.UNRESOLVED, RecordState.UNRESOLVED, None)
    if pscope in (Scope.OPAQUE_ROOT, Scope.INSIDE_OPAQUE):
        if change.is_folder:
            return c(Scope.INSIDE_OPAQUE, RecordState.METADATA_ONLY, Reason.FOLDER)
        if is_marker_child(change.name):
            # kept (no card) so that its deletion, move or rename re-opens the folder (§5.2)
            return c(Scope.INSIDE_OPAQUE, RecordState.METADATA_ONLY, item_rule(change) or Reason.KIND_NOT_ADMITTED)
        return Classification(False, Scope.INSIDE_OPAQUE, RecordState.METADATA_ONLY, None, kind, cclass)
    if change.is_folder:
        if is_dot_name(change.name):
            return c(Scope.OPAQUE_ROOT, RecordState.METADATA_ONLY, Reason.DOT_NAME)
        if (probe is not None and probe.marker_found) or _marker_opaque(previous) or is_generated_dir(change.name):
            return c(Scope.OPAQUE_ROOT, RecordState.METADATA_ONLY, Reason.OPAQUE_ROOT)
        if probe is not None:
            if is_dense(change.child_count, probe):
                return c(Scope.OPAQUE_ROOT, RecordState.METADATA_ONLY, Reason.DENSE_TREE,
                         density_count=change.child_count)
            return c(Scope.NORMAL, RecordState.METADATA_ONLY, Reason.FOLDER, density_count=change.child_count)
        if previous is not None and _density_decision_reusable(change, previous):
            return c(previous.scope, RecordState.METADATA_ONLY, previous.reason,
                     density_count=previous.probed_child_count)
        if change.child_count > DENSE_FOLDER_CHILDREN:
            return c(Scope.UNRESOLVED, RecordState.UNRESOLVED, None, probe_pending=True)
        return c(Scope.NORMAL, RecordState.METADATA_ONLY, Reason.FOLDER, density_reset=True)
    rule = item_rule(change)
    if rule is not None:
        return c(Scope.NORMAL, RecordState.METADATA_ONLY, rule)
    if kind in ADMITTED_KINDS:
        return c(Scope.NORMAL, RecordState.PENDING, None)
    return c(Scope.NORMAL, RecordState.METADATA_ONLY, Reason.KIND_NOT_ADMITTED)


def change_from_record(rec: DriveIndexRecord) -> DriveChange:
    """Re-observe a stored record (rescope, parent resolution, probe completion, upgrade)."""
    return DriveChange(item_id=rec.item_id, parent_id=rec.parent_id, name=rec.name, is_folder=rec.is_folder,
                       size_bytes=rec.size_bytes, mime_type=rec.mime_type, content_version=rec.content_version,
                       modified_at=rec.modified_at, fs_modified_at=rec.fs_modified_at, web_url=rec.web_url,
                       child_count=rec.child_count, facets=dict(rec.facets))


def _place(change: DriveChange, existing: Optional[DriveIndexRecord], place_of: PlaceOf) -> Tuple[Optional[str], int]:
    loc = photo_location(change.facets)
    if loc is None:
        return None, 0
    if (existing is not None and existing.geo_table_version == GEO_TABLE_VERSION
            and photo_location(existing.facets) == loc):
        return existing.place_name, GEO_TABLE_VERSION
    return place_of(*loc), GEO_TABLE_VERSION


def _observable(rec: DriveIndexRecord) -> Tuple[Any, ...]:
    return (rec.parent_id, rec.name, rec.is_folder, rec.size_bytes, rec.mime_type, rec.web_url, rec.child_count,
            dict(rec.facets), rec.kind, rec.scope, rec.state, rec.reason, rec.content_version, rec.modified_at,
            rec.fs_modified_at, rec.effective_date, rec.place_name, rec.geo_table_version, rec.policy_version,
            rec.probe_pending, rec.rescope_pending, rec.reobserve_pending, rec.probed_child_count)


def _build(change: DriveChange, cls: Classification, existing: Optional[DriveIndexRecord],
           ctx: PlanContext, observed: bool = True) -> DriveIndexRecord:
    base = existing or DriveIndexRecord(user_id=ctx.user_id, item_id=change.item_id)
    reason = cls.reason
    # A capped record keeps its cap until a release decides again (§5.4); re-observation must not lift it.
    if (existing is not None and existing.reason is Reason.USER_CARD_CAP
            and cls.state is RecordState.METADATA_ONLY and cls.scope in (Scope.NORMAL, Scope.OPAQUE_ROOT)):
        reason = Reason.USER_CARD_CAP
    place_name, geo_version = _place(change, existing, ctx.place_of)
    carried = existing.probed_child_count if existing and not cls.probe_pending and not cls.density_reset else 0
    probed = cls.density_count or carried
    return replace(
        base, parent_id=change.parent_id, name=change.name, is_folder=change.is_folder,
        size_bytes=change.size_bytes, mime_type=change.mime_type, web_url=change.web_url,
        child_count=change.child_count, facets=dict(change.facets), kind=cls.kind,
        content_class=cls.content_class, scope=cls.scope, state=cls.state, reason=reason,
        content_version=change.content_version, modified_at=change.modified_at,
        fs_modified_at=change.fs_modified_at, effective_date=effective_date(change, ctx.now),
        place_name=place_name, geo_table_version=geo_version, policy_version=POLICY_VERSION,
        card_rank=card_rank_of(cls.content_class), name_tokens=name_keys(change.name),
        content_hash=content_hash_of(change), probe_pending=cls.probe_pending, probed_child_count=probed,
        seen_epoch=_stamp(existing, ctx, observed),
    )


def _stamp(existing: Optional[DriveIndexRecord], ctx: PlanContext, observed: bool) -> int:
    """Only an observation stamps the epoch (§7.5); a re-plan of a stored record keeps its own (retro F10)."""
    return ctx.epoch if observed or existing is None else existing.seen_epoch


def _reopened_marker_parent(parent: DriveIndexRecord, ctx: PlanContext) -> DriveIndexRecord:
    """A marker child left a repository root: the folder becomes walkable again (§5.2)."""
    big = parent.child_count > DENSE_FOLDER_CHILDREN
    rec = replace(parent, scope=Scope.UNRESOLVED if big else Scope.NORMAL,
                  state=RecordState.UNRESOLVED if big else RecordState.METADATA_ONLY,
                  reason=None if big else Reason.FOLDER, probe_pending=big, probed_child_count=0,
                  rescope_pending=True, cascade_cursor=None, reobserve_pending=True, reobserve_cursor=None,
                  attempts=0)
    return schedule(rec, ctx.now, first_seen=False)


def _some(rec: Optional[DriveIndexRecord]) -> Tuple[DriveIndexRecord, ...]:
    return (rec,) if rec is not None else ()


def _marker_left(change: DriveChange, existing: Optional[DriveIndexRecord],
                 previous_parent: Optional[DriveIndexRecord]) -> bool:
    """A `.git`/`pyvenv.cfg` left its folder: deleted, moved away or renamed (§5.2 re-scoping)."""
    if existing is None or previous_parent is None or not is_marker_child(existing.name):
        return False
    if not (previous_parent.scope is Scope.OPAQUE_ROOT and previous_parent.reason is Reason.OPAQUE_ROOT):
        return False
    if previous_parent.state is RecordState.DELETING:
        return False   # a dying folder is never re-opened: its tombstone was already applied (retro F1)
    return change.deleted or change.parent_id != existing.parent_id or not is_marker_child(change.name)


def plan_observation(change: DriveChange, existing: Optional[DriveIndexRecord], parent: Optional[DriveIndexRecord],
                     probe: Optional[FolderProbe], ctx: PlanContext,
                     previous_parent: Optional[DriveIndexRecord] = None, *,
                     observed: bool = True) -> ObservationPlan:
    """One feed entry (or a re-observed record) -> what to write. Idempotent: the last occurrence wins (§7.2).

    `parent` is the record of change.parent_id; `previous_parent` that of existing.parent_id (for a
    deletion, a move or a rename of a marker child). The root is neither. `observed=False`: a re-plan of a
    stored record (rescope, resolution, probe, upgrade, release), which keeps the record's seen_epoch so a
    sweep still removes it when the drive no longer has it (§7.5)."""
    if change.item_id == ctx.root_item_id:
        return EMPTY
    reopened: Optional[DriveIndexRecord] = None
    if previous_parent is not None and _marker_left(change, existing, previous_parent):
        reopened = _reopened_marker_parent(previous_parent, ctx)
        if parent is not None and parent.item_id == previous_parent.item_id:
            parent = reopened

    if change.deleted:
        if existing is None:
            return EMPTY
        if existing.is_folder:
            dying = replace(existing, state=RecordState.DELETING, rescope_pending=False, probe_pending=False,
                            reobserve_pending=False, reobserve_cursor=None, has_card=False, card_hash=None,
                            seen_epoch=_stamp(existing, ctx, observed))   # invariant: a deleting record carries no card
            return ObservationPlan(upsert=schedule(dying, ctx.now, first_seen=False), parent_updates=_some(reopened))
        return ObservationPlan(delete_item_id=existing.item_id, parent_updates=_some(reopened))

    closed: Optional[DriveIndexRecord] = None
    effective_parent = parent
    if (is_marker_child(change.name) and parent is not None and parent.is_folder
            and parent.scope is Scope.NORMAL):
        closed = schedule(replace(parent, scope=Scope.OPAQUE_ROOT, reason=Reason.OPAQUE_ROOT,
                                  rescope_pending=True, cascade_cursor=None), ctx.now, first_seen=False)
        effective_parent = closed
        if reopened is not None and reopened.item_id == parent.item_id:
            reopened = None   # the folder closed again in the same observation
    parent_updates = _some(reopened) + _some(closed)

    pscope = parent_scope(change.parent_id, effective_parent, ctx.root_item_id)
    cls = classify(change, pscope, probe, existing)
    if not cls.stored:
        return ObservationPlan(delete_item_id=existing.item_id if existing else None, parent_updates=parent_updates)

    revived = existing is not None and existing.state is RecordState.DELETING
    rec = _build(change, cls, existing, ctx, observed)
    if existing is not None and rec.is_folder and existing.scope is not rec.scope:
        rec = replace(rec, rescope_pending=True, cascade_cursor=None)
        if existing.scope in (Scope.OPAQUE_ROOT, Scope.INSIDE_OPAQUE) and rec.scope is Scope.NORMAL:
            rec = replace(rec, reobserve_pending=True, reobserve_cursor=None)   # its subtree was never stored
    if revived and rec.is_folder:
        # descendants the cascade already removed come back (§7.6)
        rec = replace(rec, reobserve_pending=True, reobserve_cursor=None, attempts=0)

    if existing is not None and not revived and _observable(existing) == _observable(rec):
        stamped = existing
        if observed and ctx.phase is EpochPhase.ENUMERATING and existing.seen_epoch < ctx.epoch:
            stamped = replace(existing, seen_epoch=ctx.epoch)
        if existing.due_at is None and _has_work(existing):
            # nothing observable changed, but a version constant did (§12.3): the card is stale (review A B1)
            stamped = schedule(stamped, ctx.now, first_seen=False)
        if stamped is existing:
            return ObservationPlan(parent_updates=parent_updates)
        return ObservationPlan(upsert=stamped, parent_updates=parent_updates)
    first_seen = existing is None or not existing.has_card
    return ObservationPlan(upsert=schedule(rec, ctx.now, first_seen=first_seen), parent_updates=parent_updates)


def _card_due(rec: DriveIndexRecord) -> bool:
    return card_eligible(rec) and rec.card_hash != card_hash(rec)


def _card_drop_due(rec: DriveIndexRecord) -> bool:
    return rec.has_card and not card_eligible(rec) and rec.state is not RecordState.DELETING


def schedule(rec: DriveIndexRecord, now: datetime, *, first_seen: bool) -> DriveIndexRecord:
    """Sets due_at/priority from the work the record still carries (§7.8); clears them when none is left."""
    works = _works(rec, first_seen)
    if not works:
        return replace(rec, due_at=None, priority=None)
    return replace(rec, due_at=now, priority=int(min(works)))


def _works(rec: DriveIndexRecord, first_seen: bool) -> List[Priority]:
    works: List[Priority] = []
    if (rec.probe_pending or rec.rescope_pending or rec.state is RecordState.DELETING
            or rec.state is RecordState.UNRESOLVED or _card_drop_due(rec)):
        works.append(Priority.CASCADE)
    if rec.reobserve_pending:
        works.append(Priority.FIRST_SEEN)   # §7.8 class 2: "re-observed subtrees"
    if _card_due(rec):
        works.append(Priority.FIRST_SEEN if first_seen else Priority.EDIT)
    return works


def _has_work(rec: DriveIndexRecord) -> bool:
    return bool(_works(rec, first_seen=False))


def with_card(rec: DriveIndexRecord, hash_: str, embedding_space: str, now: datetime) -> DriveIndexRecord:
    done = replace(rec, card_hash=hash_, has_card=True, card_version=CARD_VERSION, embedding_space=embedding_space,
                   attempts=0)
    return schedule(done, now, first_seen=False)


def without_card(rec: DriveIndexRecord, now: datetime) -> DriveIndexRecord:
    return schedule(replace(rec, card_hash=None, has_card=False), now, first_seen=False)


def capped(rec: DriveIndexRecord, now: datetime) -> DriveIndexRecord:
    """No card headroom for this record (§5.4): metadata only, card dropped. A deleting record is left alone."""
    if rec.state is RecordState.DELETING:
        return rec
    return schedule(replace(rec, state=RecordState.METADATA_ONLY, reason=Reason.USER_CARD_CAP, card_hash=None,
                            has_card=False), now, first_seen=False)


def backoff(attempts: int) -> timedelta:
    delay: timedelta = min(RETRY_BASE * (2 ** attempts), RETRY_CAP)
    return delay


def with_retry(rec: DriveIndexRecord, now: datetime) -> DriveIndexRecord:
    """A per-item provider call failed (§8.2): back off in the retry class."""
    attempts = rec.attempts + 1
    return replace(rec, attempts=attempts, due_at=now + backoff(attempts), priority=int(Priority.RETRY))


def stale_versions(rec: DriveIndexRecord) -> bool:
    """Decided or carded under an older constant (§12.3)."""
    if rec.policy_version != POLICY_VERSION:
        return True
    if rec.has_card and rec.card_version != CARD_VERSION:
        return True
    return photo_location(rec.facets) is not None and rec.geo_table_version != GEO_TABLE_VERSION


def access_action(enabled: bool, state: Optional[DriveSyncState], connection: DriveConnection,
                  now: datetime) -> AccessAction:
    """The tick's gate (§7.1 step 0, §7.6). Keyed on facts only, never on an exception type."""
    if not enabled:
        return AccessAction.PURGE_OPT_OUT if state is not None else AccessAction.NOOP_DISABLED
    if state is not None and state.purging:
        return AccessAction.PURGE_THEN_RESTART   # a started purge always finishes, then onboarding (review A M1)
    lost = not connection.connected or connection.needs_reconnect
    if lost:
        if state is None:
            return AccessAction.NOOP_DISABLED
        if state.access_lost_at is not None and now >= state.access_lost_at + ACCESS_LOSS_TTL:
            return AccessAction.PURGE_EXPIRED
        return AccessAction.RECORD_LOSS
    if state is not None and connection.drive_id and state.drive_id and connection.drive_id != state.drive_id:
        return AccessAction.PURGE_DRIVE_SWITCH
    if state is not None and state.access_lost_at is not None:
        if now < state.access_lost_at + ACCESS_LOSS_TTL:
            return AccessAction.RESUME_AFTER_REGAIN
        return AccessAction.PURGE_THEN_RESTART
    return AccessAction.PROCEED


def is_stale(state: DriveSyncState, oldest_edit_due_at: Optional[datetime], now: datetime) -> bool:
    """Staleness criterion (§12.1): no clean tick, or a class-0 record overdue, for STALE_ALERT_AFTER."""
    if state.access_lost_at is not None:
        return False
    since = state.last_success_at or state.created_at
    if since is None:
        return False   # no tick has run yet: no baseline to be stale against
    if now - since > STALE_ALERT_AFTER:
        return True
    return oldest_edit_due_at is not None and now - oldest_edit_due_at > STALE_ALERT_AFTER

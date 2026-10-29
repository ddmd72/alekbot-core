"""
Drive index planner (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §3.6, §5.2, §7). Pure: every decision the
tick makes lives here and is tested without Firestore, the drive provider or a clock. The service fetches, calls
these functions and writes what they return.

Public interface:
    PlanContext, Classification, ObservationPlan, AccessAction
    parent_scope(parent_id, parent, root_item_id, mirror_root_ids=frozenset()) -> Optional[Scope]
    mirror_root_classification(change) -> Classification                                   (§27.9.8)
    needs_probe(change, pscope, previous) -> bool
    classify(change, pscope, probe, previous) -> Classification
    plan_observation(change, existing, parent, probe, ctx, previous_parent=None, *, observed=True,
                     nudged=False) -> ObservationPlan
    change_from_record(rec) -> DriveChange
    schedule(rec, now, *, first_seen) -> DriveIndexRecord
    content_due(rec, now=None) -> bool / triage_due(rec, now=None) -> bool
    quiet_over(rec, now) -> bool                                                            (§7.3)
    valid_verdict(rec, content_version) -> Optional[TriageVerdict]
    generation_started(rec, now) / indexed(rec, *, outcome, generation, chunk_count, now)
    deferred(rec, until, now, *, maintenance=False) / next_month_start(now) -> datetime
    demoted(rec, reason, now, *, outcome=None, chunk_count=0) / content_retry(rec, reason, now)
    content_requeued(rec, now) -> DriveIndexRecord; FAILED_REASONS                 (the owner's requeue script)
    triaged(rec, result, now) -> DriveIndexRecord;  attempt_crashed(rec, now) -> bool
    month_key(now) / usage_now(state, now) / with_usage(state, now, *, pages=0, images=0)
    model_available(state, now) / model_outage(state, now) / model_recovered(state)
    can_start_file(elapsed_s, *, routed) -> bool / can_start_reembed(elapsed_s) -> bool
    with_card(rec, card_hash, embedding_space, now) / without_card(rec, now) / capped(rec, now) / released(rec, now)
    card_refused(rec, now) -> DriveIndexRecord
    drops_readable(before, after) -> bool                                                    (§7.4)
    with_retry(rec, now) -> DriveIndexRecord
    access_action(enabled, state, connection, now) -> AccessAction
    is_stale(state, oldest_edit_due_at, now) -> bool
    stale_versions(rec, *, embedding_space=None) -> bool
    month_parked(rec, now, *, embedding_space) -> bool                                       (§12.3, §24.7)
    reembed_in_space(rec, embedding_space) -> DriveIndexRecord
    chunks_reembedded(rec, generation, embedding_space, now) -> DriveIndexRecord           (§12.3)
    chunks_refused(rec, embedding_space, now) -> DriveIndexRecord                            (§8.2, §12.3)
    extractor_stale(rec, versions) -> bool / reextraction_due(rec) -> DriveIndexRecord      (§9, §12.3)
    readme_read(rec, item_id, version, text, now, child_names=()) -> DriveIndexRecord      (§5.2, §25.3)
    structure_stale(rec, *, embedding_space) -> bool                                         (§12.3)

Files the bot cannot open (§26.2, D33): `inaccessible(rec)` (drive_index) is the state; the planner holds no content or
triage work for such a record, makes it due for one id read on an observation of it or its parent (at most once per
INACCESSIBLE_RETRY_MIN, Δ51) and otherwise every INACCESSIBLE_RETRY_SLOW (OQ-C1), in class 3:
    marked_inaccessible(rec, now) / access_rechecked(rec, now) / access_observed(rec, now) / access_regained(rec, now)
    access_recheck_at(rec) -> Optional[datetime] / access_recheck_due(rec, now) -> bool

Search v2 (§25.4.9): the job plans, `about_keys` and the derived queue fields are NOT decided here but by
`drive_enrich_plan.planned`, which the tick runs on every record write (`_commit`). The planner writes the facts they
are planned from: `place_forms` with the geo v2 stamp, name keys v2, the profile's issue date first in the effective
date, every collapsed folder's README excerpt, `script_rendered`, the keys stamp of an index run, the photo fields.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, FrozenSet, List, Mapping, Optional, Tuple, Union

from .drive_capture import camera_capture, model_capture, settle
from .drive_card import (
    card_eligible, card_hash, collapsed_folder, content_hash_of, effective_date, photo_location,
    structurally_card_eligible,
)
from .drive_enrichment import DriveProfile, PhotoDescription
from .drive_extraction import TRANSCRIBE_LINK, ExtractionOutcome, TriageResult, chain_for
from .drive_index import (
    CARD_VERSION, EXTRACTION_VERSION, GEO_TABLE_VERSION, KEYS_VERSION, MIGRATION_GATE_KEY, NAME_KEYS_VERSION, POLICY_VERSION,
    STRUCTURE_POLICY_SINCE, TRIAGE_VERSION, ContentClass, DocumentBrief, DriveChange, DriveIndexRecord, DriveSyncState, EpochPhase, FolderProbe,
    Priority, Reason, RecordState, Scope, TriageVerdict, inaccessible,
)
from .drive_index_params import (
    ACCESS_LOSS_TTL, CARD_LEAD_CHARS, DENSE_FOLDER_CHILDREN, DOWNLOAD_TIMEOUT_S, EMBED_FILE_BUDGET_S,
    EXTRACT_WALL_TIMEOUT_S, FILE_START_MARGIN_S, INACCESSIBLE_RETRY_MIN, INACCESSIBLE_RETRY_SLOW, MAX_ATTEMPTS,
    MAX_DEFER, MODEL_BACKOFF_BASE_S, MODEL_BACKOFF_CAP_S, MODEL_FILE_BUDGET_S, QUIET, RETRY_BASE, RETRY_CAP,
    STALE_ALERT_AFTER, TICK_DEADLINE_S,
)
from .drive_index_policy import (
    TRANSCRIBED_KINDS, Admission, admission, card_rank_of, content_class_of, density_still_valid, is_dense, is_dot_name,
    is_generated_dir, is_marker_child, is_readme_name, item_rule, kind_of, readme_readable, searchable, size_cap,
)
from .drive_keys import name_keys_v2
from .drive_profile import brief_date_grade, issued_of, readme_excerpt
from .place_lookup import Place
from .user_drive import DriveConnection

# The place a photo's coordinates resolve to: PlaceIndex.place (its display name and `place_forms`, §25.4.3), or a
# display name alone (its only form).
PlaceOf = Callable[[float, float], Union[None, str, Place]]


@dataclass(frozen=True)
class PlanContext:
    """`root_item_id` is the App Folder's root (the drive area the index covers). `mirror_root_ids` is something else:
    the ids of the bot's own `_Transcripts` folders (§27.9.2, Δ205), the union of every root document of the user,
    current and previous, on any drive (an id of another drive matches no item of this one). Such a folder is opaque:
    it is stored without a card and nothing under it is indexed (§27.9.8)."""
    user_id: str
    root_item_id: str
    epoch: int
    phase: EpochPhase
    now: datetime
    place_of: PlaceOf
    mirror_root_ids: FrozenSet[str] = frozenset()


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
    density_reset: bool = False  # decided by size alone: an earlier density anchor no longer stands (§5.2)
    content_kept: bool = False   # the previous content outcome stands at this content and extraction version


@dataclass(frozen=True)
class ObservationPlan:
    upsert: Optional[DriveIndexRecord] = None
    delete_item_id: Optional[str] = None
    parent_updates: Tuple[DriveIndexRecord, ...] = ()   # the old and/or the new parent of a marker child
    # the item whose chunks (every generation) the service deletes before writing the plan (§3.4, §7.6): the plan
    # drops its readable generation, or deletes a record that ever started an index run
    collect_chunks: Optional[str] = None


EMPTY = ObservationPlan()
_OPAQUE = (Scope.OPAQUE_ROOT, Scope.INSIDE_OPAQUE)
# metadata_only reasons decided by reading the content (§3.4, §8.2): they stand until the content or the extractor
# changes, so a rename never re-reads a scan. KIND_NOT_ADMITTED here is an `unsupported` outcome or a sniffed kind
# the policy refuses (§8.1 step 4); one from metadata alone carries no extraction_version and never stands.
_CONTENT_REASONS = frozenset({Reason.NO_TEXT, Reason.MACHINE_TEXT, Reason.ENCRYPTED, Reason.TOO_LARGE,
                              Reason.USER_CHUNK_CAP, Reason.KIND_NOT_ADMITTED})


class AccessAction(str, Enum):
    NOOP_DISABLED = "noop_disabled"            # indexing off (or access lost) and nothing stored
    PURGE_OPT_OUT = "purge_opt_out"            # indexing off, something stored
    RECORD_LOSS = "record_loss"                # access lost within the grace period: stamp, stop
    PURGE_EXPIRED = "purge_expired"            # access lost past ACCESS_LOSS_TTL
    PURGE_DRIVE_SWITCH = "purge_drive_switch"  # a different drive is connected
    RESUME_AFTER_REGAIN = "resume_after_regain"
    PURGE_THEN_RESTART = "purge_then_restart"  # regained after the TTL: finish the purge, start epoch 1
    PROCEED = "proceed"


def parent_scope(parent_id: str, parent: Optional[DriveIndexRecord], root_item_id: str,
                 mirror_root_ids: FrozenSet[str] = frozenset()) -> Optional[Scope]:
    """Scope a child inherits; None when the parent is not known yet. The root is an implicit normal parent; a mirror
    root (§27.9.2) an implicit opaque one, so its direct children are never held unresolved while its own record is
    not stored yet (the first enumeration after a purge, §27.9.8)."""
    if parent_id and parent_id == root_item_id:
        return Scope.NORMAL
    if parent_id and parent_id in mirror_root_ids:
        return Scope.INSIDE_OPAQUE
    return parent.scope if parent is not None else None


def mirror_root_classification(change: DriveChange) -> Classification:
    """A mirror root (`PlanContext.mirror_root_ids`, §27.9.8): `inside_opaque / metadata_only / folder`, existing values,
    stored without a card (not a collapsed folder: no card, no README read). Its children inherit the opaque scope."""
    kind = kind_of(change.name, change.mime_type, change.is_folder)
    return Classification(True, Scope.INSIDE_OPAQUE, RecordState.METADATA_ONLY, Reason.FOLDER, kind,
                          content_class_of(kind))


def _density_decision_reusable(change: DriveChange, previous: Optional[DriveIndexRecord]) -> bool:
    return (previous is not None and previous.probed_child_count > 0
            and previous.policy_version >= STRUCTURE_POLICY_SINCE
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
            and rec.policy_version >= STRUCTURE_POLICY_SINCE and not is_generated_dir(rec.name))


def classify(change: DriveChange, pscope: Optional[Scope], probe: Optional[FolderProbe],
             previous: Optional[DriveIndexRecord]) -> Classification:
    kind = kind_of(change.name, change.mime_type, change.is_folder)
    cclass = content_class_of(kind)

    def c(scope: Scope, state: RecordState, reason: Optional[Reason], **kw: Any) -> Classification:
        return Classification(True, scope, state, reason, kind, cclass, **kw)

    if pscope is None or pscope is Scope.UNRESOLVED:
        # a scope hold (R3 F2-1): a file keeps the content outcome that stands, its reason in `reason` (None for
        # indexed) and its content fields as they are, so the outcome stands again once the parent resolves
        held = (previous.reason if previous is not None and not change.is_folder
                and _outcome_stands(previous, change.content_version) else None)
        return c(Scope.UNRESOLVED, RecordState.UNRESOLVED, held)
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
    adm = admission(kind)
    if adm is Admission.TRIAGE:
        verdict = valid_verdict(previous, change.content_version)
        if verdict is TriageVerdict.PHOTO:
            return c(Scope.NORMAL, RecordState.METADATA_ONLY, Reason.PHOTOGRAPH)
        if verdict is None:
            return c(Scope.NORMAL, RecordState.PENDING, None)   # triage due (§24.5)
    elif adm is Admission.CONTENT:
        if change.size_bytes > size_cap(kind):
            return c(Scope.NORMAL, RecordState.METADATA_ONLY, Reason.TOO_LARGE)
    elif adm is Admission.TRANSCRIBE:
        pass   # no download cap here: the media caps are the transcript plan's (§27.4); a stub outcome stands below
    else:
        return c(Scope.NORMAL, RecordState.METADATA_ONLY, Reason.KIND_NOT_ADMITTED)
    if previous is not None and _outcome_stands(previous, change.content_version):
        return c(Scope.NORMAL, _outcome_state(previous), previous.reason, content_kept=True)
    return c(Scope.NORMAL, RecordState.PENDING, None)


def valid_verdict(rec: Optional[DriveIndexRecord], content_version: str) -> Optional[TriageVerdict]:
    """The triage verdict that still holds: taken at the current TRIAGE_VERSION on this content version (§5.3)."""
    if (rec is None or rec.triage_verdict is None or rec.triage_version != TRIAGE_VERSION
            or rec.triaged_content_version != content_version):
        return None
    return rec.triage_verdict


def _indexed_now(rec: DriveIndexRecord) -> bool:
    """Readable chunks built from this content version by the current extraction rules."""
    return (rec.indexed_generation is not None and rec.indexed_version == rec.content_version
            and rec.extraction_version == EXTRACTION_VERSION)


def _outcome_state(rec: DriveIndexRecord) -> RecordState:
    """The content state a record holds. An unresolved file is a scope hold (R3 F2-1): its `reason` keeps the outcome
    it had — a per-file failure, a content demotion, or None with its readable generation (indexed)."""
    if rec.state is not RecordState.UNRESOLVED:
        return rec.state
    if rec.reason in _FAILED_REASONS:
        return RecordState.FAILED
    if rec.reason in _CONTENT_REASONS:
        return RecordState.METADATA_ONLY
    return RecordState.INDEXED if _indexed_now(rec) else RecordState.PENDING


def _outcome_stands(previous: DriveIndexRecord, content_version: str) -> bool:
    """A content outcome (indexed, failed, or a content demotion) decided at this content version under the
    current extraction rules (§3.4, §8.2): kept, so a rename, a policy re-plan or a spell under an unresolved parent
    never re-reads the file."""
    if previous.content_version != content_version or previous.extraction_version != EXTRACTION_VERSION:
        return False
    state = _outcome_state(previous)
    if state is RecordState.INDEXED:
        return _indexed_now(previous)
    return state is RecordState.FAILED or (state is RecordState.METADATA_ONLY
                                           and previous.reason in _CONTENT_REASONS)


def change_from_record(rec: DriveIndexRecord) -> DriveChange:
    """Re-observe a stored record (rescope, parent resolution, probe completion, upgrade)."""
    return DriveChange(item_id=rec.item_id, parent_id=rec.parent_id, name=rec.name, is_folder=rec.is_folder,
                       size_bytes=rec.size_bytes, mime_type=rec.mime_type, content_version=rec.content_version,
                       modified_at=rec.modified_at, fs_modified_at=rec.fs_modified_at, web_url=rec.web_url,
                       child_count=rec.child_count, facets=dict(rec.facets))


def _place(change: DriveChange, existing: Optional[DriveIndexRecord],
           place_of: PlaceOf) -> Tuple[Optional[str], Tuple[str, ...], int]:
    """(place_name, place_forms, geo stamp): written together (§25.4.3), so a stamp never stands without its forms; a
    stored place at this stamp and location is kept."""
    loc = photo_location(change.facets)
    if loc is None:
        return None, (), 0
    if (existing is not None and existing.geo_table_version == GEO_TABLE_VERSION
            and photo_location(existing.facets) == loc):
        return existing.place_name, existing.place_forms, GEO_TABLE_VERSION
    found = place_of(*loc)
    if isinstance(found, Place):
        return found.name, tuple(found.forms), GEO_TABLE_VERSION
    return found, ((found,) if found else ()), GEO_TABLE_VERSION


def _observable(rec: DriveIndexRecord) -> Tuple[Any, ...]:
    return (rec.parent_id, rec.name, rec.is_folder, rec.size_bytes, rec.mime_type, rec.web_url, rec.child_count,
            dict(rec.facets), rec.kind, rec.scope, rec.state, rec.reason, rec.content_version, rec.modified_at,
            rec.fs_modified_at, rec.effective_date, rec.place_name, rec.geo_table_version, rec.policy_version,
            rec.probe_pending, rec.rescope_pending, rec.reobserve_pending, rec.probed_child_count, rec.card_capped,
            rec.card_refused, rec.name_keys_version, rec.indexed_generation, rec.lead_text, rec.brief, rec.caption,
            rec.dirty_since, rec.readme_pending, rec.readme_item_id, rec.readme_version, rec.chunk_reembed_pending,
            rec.place_forms, rec.readme_excerpt, rec.readme_excerpt_version, rec.photo_description,
            rec.photo_objects, rec.photo_scene, rec.photo_scene_labels, rec.visible_text)


def _build(change: DriveChange, cls: Classification, existing: Optional[DriveIndexRecord],
           ctx: PlanContext, observed: bool = True) -> DriveIndexRecord:
    base = existing or DriveIndexRecord(user_id=ctx.user_id, item_id=change.item_id)
    place_name, place_forms, geo_version = _place(change, existing, ctx.place_of)
    carried = existing.probed_child_count if existing and not cls.probe_pending and not cls.density_reset else 0
    probed = cls.density_count or carried
    if cls.state is RecordState.METADATA_ONLY and not cls.content_kept:
        # entering metadata_only clears the readable generation (the service collects its chunks) and what the
        # content gave the card (§3.4); a content demotion that stands keeps its own fields, and a collapsed folder
        # that stays one keeps its README excerpt (§5.2, §25.3). Decided from metadata, it carries no extraction stamp:
        # nothing was read, so a later observation that admits the file again reads it (a rename away and back)
        base = replace(base, indexed_generation=None, brief=None, extraction_version=0, extracted_by=None,
                       chunk_space="", chunk_reembed_pending=False, capture="", capture_source="")
        if not (existing is not None and collapsed_folder(existing) and _collapsed(change, cls)):
            base = replace(base, lead_text="", readme_pending=False, readme_item_id=None, readme_version="",
                           readme_excerpt="", readme_excerpt_version="")
    if admission(cls.kind) is not Admission.TRIAGE:
        # a photograph's verdict and its description live together: one dropped without the other would keep the
        # verdict and lose the description for good, so an image that comes back to triage is triaged again (§24.5,
        # T-m3); the kind leaving triage clears the photo fields (§25.4.9)
        base = _without_photo(replace(base, caption=""))
        if base.triage_verdict is TriageVerdict.PHOTO:
            base = replace(base, triage_verdict=None, triage_version=0, triaged_content_version="")
    if cls.state is not RecordState.PENDING:
        # no content work is left: its quiet period, deferral and started attempt end with it
        base = replace(base, dirty_since=None, defer_until=None, defer_priority=None, attempt_started_at=None)
    rec = replace(
        base, parent_id=change.parent_id, name=change.name, is_folder=change.is_folder,
        size_bytes=change.size_bytes, mime_type=change.mime_type, web_url=change.web_url,
        child_count=change.child_count, facets=dict(change.facets), kind=cls.kind,
        content_class=cls.content_class, scope=cls.scope, state=cls.state, reason=cls.reason,
        content_version=change.content_version, modified_at=change.modified_at,
        fs_modified_at=change.fs_modified_at,
        effective_date=_effective(change, base.brief, ctx.now, base.profile, cls.kind),
        place_name=place_name, place_forms=place_forms, geo_table_version=geo_version, policy_version=POLICY_VERSION,
        card_rank=card_rank_of(cls.content_class),
        name_tokens=name_keys_v2(change.name) if searchable(cls.reason) else (), name_keys_version=NAME_KEYS_VERSION,
        content_hash=content_hash_of(change), probe_pending=cls.probe_pending, probed_child_count=probed,
        seen_epoch=_stamp(existing, ctx, observed),
        # DRIVE_FILE_NOTES_RFC §5.1 a (review I2): a record this observation creates looks its owner note up in the
        # one write gate (`_commit`); an existing record carries its copy of the note across (`replace(existing)`)
        note_lookup_due=True if existing is None else base.note_lookup_due,
    )
    # A capped record keeps its cap until a release decides again (§5.4); one that can hold no card drops it.
    rec = replace(rec, card_capped=rec.card_capped and structurally_card_eligible(rec))
    if _readme_due(existing, rec):
        rec = replace(rec, readme_pending=True)
    return replace(rec, card_refused=_refusal_stands(existing, rec))


def _without_photo(rec: DriveIndexRecord) -> DriveIndexRecord:
    """The photo fields cleared (§25.4.9: a text verdict, or the kind leaving triage)."""
    return replace(rec, photo_description="", photo_objects=(), photo_scene="", photo_scene_labels=(),
                   visible_text="")


def _collapsed(change: DriveChange, cls: Classification) -> bool:
    return change.is_folder and cls.scope is Scope.OPAQUE_ROOT


def _readme_due(existing: Optional[DriveIndexRecord], rec: DriveIndexRecord) -> bool:
    """Every collapsed folder (any opaque_root reason, §25.3) reads its README when it becomes one, when its child
    count moves (a README added, removed or renamed) or when its record predates STRUCTURE_POLICY_SINCE (a media-only POLICY_VERSION bump does not count); never on a PROFILE_VERSION bump, which
    re-profiles from the stored excerpt; only once text content is admitted (§5.2)."""
    if not collapsed_folder(rec) or not readme_readable():
        return False
    return (existing is None or not collapsed_folder(existing) or existing.child_count != rec.child_count
            or existing.policy_version < STRUCTURE_POLICY_SINCE)


def _readme_touched(change: DriveChange, parent: Optional[DriveIndexRecord],
                    now: datetime) -> Tuple[DriveIndexRecord, ...]:
    """A README directly inside a collapsed folder changed: the folder reads it again (§5.2, §25.3). Its own record is
    never kept (files inside an opaque root get none)."""
    if (parent is None or change.is_folder or change.parent_id != parent.item_id or not collapsed_folder(parent)
            or parent.readme_pending or not is_readme_name(change.name) or not readme_readable()
            or change.content_version == parent.readme_version):
        return ()
    return (schedule(replace(parent, readme_pending=True), now, first_seen=not parent.has_card),)


def _effective(change: DriveChange, brief: Optional[DocumentBrief], now: datetime,
               profile: Optional[DriveProfile] = None, kind: str = "") -> datetime:
    """The profile's verified issue date first (§25.4.1, D13 extended), else the brief's date (D13), else §3.1. A
    transcribed recording (`kind` audio/video) always takes §3.1: a date said aloud never re-dates it (§27.8 a, G38)."""
    if kind in TRANSCRIBED_KINDS:
        return effective_date(change, now)
    issued = issued_of(profile) if profile is not None else None
    if issued is not None:
        return datetime(issued.year, issued.month, issued.day, tzinfo=timezone.utc)
    if brief is not None and brief.held_date is not None:
        return brief.held_date
    return effective_date(change, now)


def _refusal_stands(existing: Optional[DriveIndexRecord], rec: DriveIndexRecord) -> bool:
    """A refused card stays refused while the card text is the one refused (§8.2)."""
    return (existing is not None and existing.card_refused and not existing.has_card
            and existing.card_hash is not None and card_eligible(rec) and existing.card_hash == card_hash(rec))


def _stamp(existing: Optional[DriveIndexRecord], ctx: PlanContext, observed: bool) -> int:
    """Only an observation stamps the epoch (§7.5); a re-plan of a stored record keeps its own, so a record
    the drive no longer has is still swept."""
    return ctx.epoch if observed or existing is None else existing.seen_epoch


def _reopened_marker_parent(parent: DriveIndexRecord, ctx: PlanContext) -> DriveIndexRecord:
    """A marker child left a repository root: the folder becomes walkable again (§5.2)."""
    big = parent.child_count > DENSE_FOLDER_CHILDREN
    rec = replace(parent, scope=Scope.UNRESOLVED if big else Scope.NORMAL,
                  state=RecordState.UNRESOLVED if big else RecordState.METADATA_ONLY,
                  reason=None if big else Reason.FOLDER, probe_pending=big, probed_child_count=0,
                  rescope_pending=True, cascade_cursor=None, reobserve_pending=True, reobserve_cursor=None,
                  attempts=0, lead_text="", readme_pending=False, readme_item_id=None, readme_version="",
                  readme_excerpt="", readme_excerpt_version="")
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
        return False   # a dying folder is never re-opened: its tombstone was already applied (§7.6)
    return change.deleted or change.parent_id != existing.parent_id or not is_marker_child(change.name)


def plan_observation(change: DriveChange, existing: Optional[DriveIndexRecord], parent: Optional[DriveIndexRecord],
                     probe: Optional[FolderProbe], ctx: PlanContext,
                     previous_parent: Optional[DriveIndexRecord] = None, *,
                     observed: bool = True, nudged: bool = False) -> ObservationPlan:
    """One feed entry (or a re-observed record) -> what to write. Idempotent: the last occurrence wins (§7.2).

    `parent` is the record of change.parent_id; `previous_parent` that of existing.parent_id (for a
    deletion, a move or a rename of a marker child). The root is neither. `observed=False`: a re-plan of a
    stored record (rescope, resolution, probe, upgrade, release), which keeps the record's seen_epoch so a
    sweep still removes it when the drive no longer has it (§7.5). `nudged=True`: the bot's own atomic write
    (§7.1 step 1), read with QUIET = 0, so its content is due at once (§7.3).

    A record the bot cannot open (§26.2) that this observation sees again is flagged observed: its next read is due
    INACCESSIBLE_RETRY_MIN after the last one (Δ51); a re-plan (`observed=False`) is no observation."""
    plan = _collecting(_plan(change, existing, parent, probe, ctx, previous_parent, observed, nudged), existing)
    if not (observed and existing is not None and inaccessible(existing) and not change.deleted
            and plan.delete_item_id != existing.item_id):
        return plan
    base = plan.upsert if plan.upsert is not None and plan.upsert.item_id == existing.item_id else existing
    if not inaccessible(base) or base.inaccessible_reobserved:
        return plan
    return replace(plan, upsert=access_observed(base, ctx.now))


def drops_readable(before: Optional[DriveIndexRecord], after: DriveIndexRecord) -> bool:
    """The write of `after` over `before` leaves the record with no readable generation where it had one: the
    writer collects that generation's chunks (§3.4, §7.4; R3 F2-2). Every commit path decides it here; a run that
    commits another generation collects the rest itself (`delete_chunks_except`)."""
    return before is not None and before.indexed_generation is not None and after.indexed_generation is None


def _collecting(plan: ObservationPlan, existing: Optional[DriveIndexRecord]) -> ObservationPlan:
    if existing is None:
        return plan
    deleted = plan.delete_item_id == existing.item_id and (existing.indexed_generation is not None
                                                           or existing.index_generation > 0)
    dropped = plan.upsert is not None and drops_readable(existing, plan.upsert)
    return replace(plan, collect_chunks=existing.item_id) if deleted or dropped else plan


def _plan(change: DriveChange, existing: Optional[DriveIndexRecord], parent: Optional[DriveIndexRecord],
          probe: Optional[FolderProbe], ctx: PlanContext, previous_parent: Optional[DriveIndexRecord],
          observed: bool, nudged: bool) -> ObservationPlan:
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
        closing = replace(parent, scope=Scope.OPAQUE_ROOT, reason=Reason.OPAQUE_ROOT, rescope_pending=True,
                          cascade_cursor=None, reobserve_pending=False, reobserve_cursor=None)
        closing = replace(closing, readme_pending=_readme_due(parent, closing))
        closed = schedule(closing, ctx.now, first_seen=False)
        effective_parent = closed
        if reopened is not None and reopened.item_id == parent.item_id:
            reopened = None   # the folder closed again in the same observation
    parent_updates = _some(reopened) + _some(closed)

    if change.item_id in ctx.mirror_root_ids:
        cls = mirror_root_classification(change)   # the bot's own folder: opaque whatever its parent (§27.9.8)
    else:
        pscope = parent_scope(change.parent_id, effective_parent, ctx.root_item_id, ctx.mirror_root_ids)
        cls = classify(change, pscope, probe, existing)
    if not cls.stored:
        parent_updates += _readme_touched(change, effective_parent, ctx.now)
        return ObservationPlan(delete_item_id=existing.item_id if existing else None, parent_updates=parent_updates)

    revived = existing is not None and existing.state is RecordState.DELETING
    rec = _build(change, cls, existing, ctx, observed)
    rec = _kind_changed(rec, existing)
    rec = _quiet_period(rec, existing, ctx.now, nudged)
    if existing is not None and rec.is_folder and existing.scope is not rec.scope:
        rec = replace(rec, rescope_pending=True, cascade_cursor=None)
        if existing.scope in (Scope.OPAQUE_ROOT, Scope.INSIDE_OPAQUE) and rec.scope is Scope.NORMAL:
            rec = replace(rec, reobserve_pending=True, reobserve_cursor=None)   # its subtree was never stored
    if revived and rec.is_folder:
        # descendants the cascade already removed come back (§7.6)
        rec = replace(rec, reobserve_pending=True, reobserve_cursor=None, attempts=0)
    if rec.reobserve_pending and rec.scope in _OPAQUE:
        # an opaque folder's subtree is never stored (§5.2); an unresolved one keeps it until its probe decides
        rec = replace(rec, reobserve_pending=False, reobserve_cursor=None)

    if existing is not None and not revived and _observable(existing) == _observable(rec):
        stamped = existing
        if observed and ctx.phase is EpochPhase.ENUMERATING and existing.seen_epoch < ctx.epoch:
            stamped = replace(existing, seen_epoch=ctx.epoch)
        if existing.due_at is None and _has_work(existing):
            # nothing observable changed, but a version constant did (§12.3): the card is stale
            stamped = schedule(stamped, ctx.now, first_seen=False)
        if stamped is existing:
            return ObservationPlan(parent_updates=parent_updates)
        return ObservationPlan(upsert=stamped, parent_updates=parent_updates)
    first_seen = existing is None or not existing.has_card
    return ObservationPlan(upsert=schedule(rec, ctx.now, first_seen=first_seen), parent_updates=parent_updates)


def _kind_changed(rec: DriveIndexRecord, existing: Optional[DriveIndexRecord]) -> DriveIndexRecord:
    """A content kind change (a backed-off `.jpg` renamed to `x.pdf`) is another extractor chain: its work starts
    fresh, like a new version (§8.2, arbiter R1-4). The attempts, the started attempt, the backoff and any deferral
    counted the old kind's work (content, triage or its rendition retries); a triage verdict stays, bound to the
    content version as always."""
    if existing is None or existing.kind == rec.kind:
        return rec
    return replace(rec, attempts=0, attempt_started_at=None, defer_until=None, defer_priority=None, model_outages=0)


def _quiet_period(rec: DriveIndexRecord, existing: Optional[DriveIndexRecord], now: datetime,
                  nudged: bool) -> DriveIndexRecord:
    """§7.3: a content change on a stored record waiting for content marks it dirty (once) and restarts its
    per-file attempts (§8.2); a nudge is the bot's own atomic write, so it is never in a quiet period. A file held
    under an unresolved parent (R3 F2-1) that changes meanwhile restarts them too, so a new version never inherits
    the old one's attempts or charged reader outages (F1-1) when the parent resolves."""
    new_version = existing is not None and existing.content_version != rec.content_version
    if rec.state is RecordState.UNRESOLVED and not rec.is_folder and new_version:
        return replace(rec, attempts=0, defer_until=None, defer_priority=None, model_outages=0)
    if rec.state is not RecordState.PENDING:
        return rec
    if new_version:
        # a new edit restarts the work: its attempts and any deferral of the old version end
        rec = replace(rec, attempts=0, defer_until=None, defer_priority=None, model_outages=0)
    if nudged:
        return replace(rec, dirty_since=None)
    if new_version:
        return replace(rec, dirty_since=rec.dirty_since or now)
    return rec


def _held(rec: DriveIndexRecord, now: Optional[datetime]) -> bool:
    return now is not None and rec.defer_until is not None and now < rec.defer_until


def content_due(rec: DriveIndexRecord, now: Optional[datetime] = None) -> bool:
    """An index run is due (§8.1): pending, its content admitted (a content kind, or an image with a valid `text`
    verdict), and not yet indexed at this content version under the current extraction rules. A crashed or
    retried run is still due; an indexed record is not. With `now`: and not deferred past `now`. A card-capped record
    holds its content work until released: search never shows content without a card (§5.4; R3 F1-2)."""
    if (rec.state is not RecordState.PENDING or rec.card_capped or inaccessible(rec) or _indexed_now(rec)
            or _held(rec, now)):
        return False   # an inaccessible record holds its content work until the bot can read it again (§26.2)
    adm = admission(rec.kind)   # a TRANSCRIBE kind never reaches the index step (§27.3, Δ95)
    return adm is Admission.CONTENT or (adm is Admission.TRIAGE
                                        and valid_verdict(rec, rec.content_version) is TriageVerdict.TEXT)


def triage_due(rec: DriveIndexRecord, now: Optional[datetime] = None) -> bool:
    """An image waits for a verdict at the current TRIAGE_VERSION on its content version (§24.5). With `now`: and
    not deferred past `now`. A card-capped image is not triaged until released (§5.4; R3 F1-2)."""
    return (rec.state is RecordState.PENDING and not rec.card_capped and not inaccessible(rec)
            and admission(rec.kind) is Admission.TRIAGE and valid_verdict(rec, rec.content_version) is None
            and not _held(rec, now))


def quiet_over(rec: DriveIndexRecord, now: datetime) -> bool:
    """§7.3: the record's content or triage work may start at `now` — it is not dirty, or its quiet period (QUIET
    untouched, at most MAX_DEFER dirty) has ended. A record due for other work (a card, a re-embed) is not thereby due
    for its content."""
    return content_due_at(rec, now) <= now


def content_due_at(rec: DriveIndexRecord, now: datetime) -> datetime:
    """§7.3: an edit waits until untouched for QUIET or dirty for MAX_DEFER; anything not dirty (first sight, a
    nudge, an upgrade, a release) is due at once."""
    if rec.dirty_since is None:
        return now
    bound = rec.dirty_since + MAX_DEFER
    return min(rec.modified_at + QUIET, bound) if rec.modified_at is not None else bound


def _card_due(rec: DriveIndexRecord) -> bool:
    return card_eligible(rec) and rec.card_hash != card_hash(rec)


def _card_drop_due(rec: DriveIndexRecord) -> bool:
    return rec.has_card and not card_eligible(rec) and rec.state is not RecordState.DELETING


def schedule(rec: DriveIndexRecord, now: datetime, *, first_seen: bool) -> DriveIndexRecord:
    """Sets due_at/priority from the work the record still carries (§7.8); clears them when none is left. Several
    works: the earliest due time and the most urgent class."""
    works = _works(rec, first_seen, now)
    if not works:
        return replace(rec, due_at=None, priority=None)
    return replace(rec, due_at=min(at for _, at in works), priority=int(min(p for p, _ in works)))


def _works(rec: DriveIndexRecord, first_seen: bool, now: datetime) -> List[Tuple[Priority, datetime]]:
    works: List[Tuple[Priority, datetime]] = []
    if (rec.probe_pending or rec.rescope_pending or rec.state is RecordState.DELETING
            or rec.state is RecordState.UNRESOLVED or _card_drop_due(rec)):
        works.append((Priority.CASCADE, now))
    closed = inaccessible(rec)
    if rec.reobserve_pending and not closed:     # a folder the bot cannot list waits for access (§26.2)
        works.append((Priority.FIRST_SEEN, now))   # §7.8 class 2: "re-observed subtrees"
    if rec.readme_pending and not closed:
        works.append((Priority.MAINTENANCE, now))  # an enrichment of the repository card (§5.2)
    recheck = access_recheck_at(rec)
    if recheck is not None:
        works.append((Priority.MAINTENANCE, recheck))   # one id read: is it readable again? (§26.2, Δ51)
    if rec.chunk_reembed_pending and rec.indexed_generation is not None:
        works.append((Priority.MAINTENANCE, now))  # readable chunks in another embedding space (§12.3)
    if rec.keys_pending and rec.indexed_generation is not None:
        works.append((Priority.MAINTENANCE, now))  # keys under another KEYS_VERSION (§25.4.5, §25.4.8)
    if _card_due(rec):
        works.append((Priority.FIRST_SEEN if first_seen else Priority.EDIT, now))
    if content_due(rec) or triage_due(rec):
        at = content_due_at(rec, now)
        cls = Priority.FIRST_SEEN if first_seen else Priority.EDIT
        if rec.defer_until is not None:   # a deferral survives any re-scheduling until the work ends
            at = max(at, rec.defer_until)
        works.append((Priority(rec.defer_priority) if rec.defer_priority is not None else cls, at))
    return works


def _has_work(rec: DriveIndexRecord) -> bool:
    return bool(_works(rec, first_seen=False, now=datetime.min))   # whether any work exists; its time is unused


def _card_attempts(rec: DriveIndexRecord) -> int:
    """A card outcome ends the card's refusal count; while content or triage work is pending, `attempts` counts that
    work's per-file failures (§8.2), so a card write never restarts them."""
    return rec.attempts if content_due(rec) or triage_due(rec) else 0


def with_card(rec: DriveIndexRecord, hash_: str, embedding_space: str, now: datetime) -> DriveIndexRecord:
    done = replace(rec, card_hash=hash_, has_card=True, card_refused=False, card_version=CARD_VERSION,
                   embedding_space=embedding_space, attempts=_card_attempts(rec))
    return schedule(done, now, first_seen=False)


def card_refused(rec: DriveIndexRecord, now: datetime) -> DriveIndexRecord:
    """The provider refused this card text MAX_ATTEMPTS times: terminal (§8.2), flagged `card_refused` with the state
    left to the structure. The record keeps no card, and `card_hash` names the refused text, so the same text is not
    tried again; a new text (a rename, a facet change, a new card builder) gets a fresh try."""
    return schedule(replace(rec, card_refused=True, has_card=False, card_hash=card_hash(rec),
                            card_version=CARD_VERSION, attempts=_card_attempts(rec)), now, first_seen=False)


def without_card(rec: DriveIndexRecord, now: datetime) -> DriveIndexRecord:
    return schedule(replace(rec, card_hash=None, has_card=False), now, first_seen=False)


def capped(rec: DriveIndexRecord, now: datetime) -> DriveIndexRecord:
    """No card headroom for this record (§5.4): the card is dropped and withheld; scope and reason stay what the
    structure says, so a capped repository stays opaque. A deleting record is left alone. Content without a card is
    never shown, so the readable generation goes too (the caller collects it, drops_readable) and stops counting
    toward the chunk cap; an indexed record is pending again, its content and triage held until released (R3 F1-2)."""
    if rec.state is RecordState.DELETING:
        return rec
    state = RecordState.PENDING if rec.state is RecordState.INDEXED else rec.state
    return schedule(replace(rec, card_capped=True, card_refused=False, card_hash=None, has_card=False, state=state,
                            indexed_generation=None, chunk_space="", chunk_reembed_pending=False), now,
                    first_seen=False)


def released(rec: DriveIndexRecord, now: datetime) -> DriveIndexRecord:
    """Card headroom came back (§5.4): the cap is lifted, and the content or triage it held is due before the card."""
    return schedule(replace(rec, card_capped=False), now, first_seen=True)


def backoff(attempts: int) -> timedelta:
    delay: timedelta = min(RETRY_BASE * (2 ** attempts), RETRY_CAP)
    return delay


def with_retry(rec: DriveIndexRecord, now: datetime) -> DriveIndexRecord:
    """A per-item provider call failed (§8.2): back off in the retry class."""
    attempts = rec.attempts + 1
    return replace(rec, attempts=attempts, due_at=now + backoff(attempts), priority=int(Priority.RETRY))


def stale_versions(rec: DriveIndexRecord, *, embedding_space: Optional[str] = None) -> bool:
    """Decided, keyed or carded under an older constant (§12.3); with `embedding_space`, a card embedded in another
    space is stale too."""
    if _structure_stale(rec, embedding_space):
        return True
    if rec.extraction_version > 0 and rec.extraction_version != EXTRACTION_VERSION:       # content read before
        return True
    return rec.triage_verdict is not None and rec.triage_version != TRIAGE_VERSION


def month_parked(rec: DriveIndexRecord, now: datetime, *, embedding_space: str) -> bool:
    """Its content or triage work waits for next month's budget (§24.7) and it is stale only for what that held work
    applies (the extraction rules, its extractor link, the triage version): an upgrade pass leaves it parked, like a
    record under an unresolved parent, until the month turns (R3 F1-5). Anything else stale is planned as usual."""
    return (rec.defer_until is not None and rec.defer_until >= next_month_start(now)
            and not _structure_stale(rec, embedding_space))


def structure_stale(rec: DriveIndexRecord, *, embedding_space: Optional[str]) -> bool:
    """Stale for a version an upgrade re-plan applies without reading the file (§12.3): what a record the bot cannot
    open (§26.2) can still be brought up to date, so only this keeps an upgrade pass from applying."""
    return _structure_stale(rec, embedding_space)


def _structure_stale(rec: DriveIndexRecord, embedding_space: Optional[str]) -> bool:
    """The versions an upgrade re-plan applies at once: policy, name keys, card, embedding space, geo table."""
    if rec.policy_version != POLICY_VERSION or rec.name_keys_version != NAME_KEYS_VERSION:
        return True
    if embedding_space is not None and rec.has_card and rec.embedding_space != embedding_space:
        return True
    if embedding_space is not None and rec.indexed_generation is not None and rec.chunk_space != embedding_space:
        return True   # readable chunks embedded in another space
    if embedding_space is not None and rec.chunks_refused_space and rec.chunks_refused_space != embedding_space:
        return True   # refused by another space's model: this one gives it a fresh try (R3 F2-3)
    if (rec.has_card or rec.card_hash is not None) and rec.card_version != CARD_VERSION:   # carded, or refused
        return True
    return photo_location(rec.facets) is not None and rec.geo_table_version != GEO_TABLE_VERSION


def extractor_stale(rec: DriveIndexRecord, versions: Mapping[str, int]) -> bool:
    """§9, §12.3: the content was decided by a link (`extracted_by` = "id@version") that is no longer in the chain
    for the record's kind, or no longer at that version. `versions` maps each wired extractor id to its version."""
    if not rec.extracted_by:
        return False
    link, _, version = rec.extracted_by.rpartition("@")
    if link == TRANSCRIBE_LINK and admission(rec.kind) is Admission.TRANSCRIBE:
        return False   # its own pipeline decides (§27, G4); chain_for("audio") would call it stale on every upgrade
    return link not in chain_for(rec.kind) or str(versions.get(link, "")) != version


def reextraction_due(rec: DriveIndexRecord) -> DriveIndexRecord:
    """Its content outcome no longer stands (§9): planned again, it is pending; a readable generation keeps serving
    until the new one commits (§7.4). Only `extraction_version` moves: the outcome was decided under other rules."""
    return replace(rec, extraction_version=0)


def readme_read(rec: DriveIndexRecord, item_id: Optional[str], version: str, text: Optional[str],
                now: datetime, child_names: Tuple[str, ...] = ()) -> DriveIndexRecord:
    """A collapsed folder's README was read, found absent or given up on (§5.2, §25.3): its head and tail and up to
    README_CHILD_NAMES child names become `readme_excerpt` (the profile's input), with the README's content version.
    `text` None keeps the stored excerpt (an unchanged README, or one given up on); "" clears it. No lead text: the
    v2 card carries the folder's profile (§25.2.7)."""
    if text is None:
        excerpt, excerpt_version = rec.readme_excerpt, rec.readme_excerpt_version
    else:
        excerpt = readme_excerpt(text.strip(), child_names) if text.strip() else ""
        excerpt_version = version if excerpt else ""
    return schedule(replace(rec, readme_pending=False, readme_item_id=item_id, readme_version=version, lead_text="",
                            readme_excerpt=excerpt, readme_excerpt_version=excerpt_version, attempts=0), now,
                    first_seen=not rec.has_card)


def reembed_in_space(rec: DriveIndexRecord, embedding_space: str) -> DriveIndexRecord:
    """A card embedded in another space is due again (§12.3): its hash is cleared, so the card is scheduled like any
    changed card; the old vector stays until the new one replaces it. Readable chunks in another space are due to be
    embedded again from their stored text, into a new generation, with no download and no extraction; the old
    generation serves until the new one commits (§7.4)."""
    if rec.has_card and rec.embedding_space != embedding_space:
        rec = replace(rec, card_hash=None)
    if rec.indexed_generation is not None and rec.chunk_space != embedding_space:
        rec = replace(rec, chunk_reembed_pending=True)
    if rec.chunks_refused_space and rec.chunks_refused_space != embedding_space:
        # the refusal held for another space's model: the file is read again for this one (R3 F2-3)
        rec = replace(reextraction_due(rec), chunks_refused_space="")
    return rec


def chunks_reembedded(rec: DriveIndexRecord, generation: int, embedding_space: str,
                      now: datetime) -> DriveIndexRecord:
    """The re-embedded generation becomes readable in `embedding_space`; the caller collects every other one."""
    return schedule(replace(rec, indexed_generation=generation, chunk_space=embedding_space,
                            chunk_reembed_pending=False), now, first_seen=not rec.has_card)


def chunks_refused(rec: DriveIndexRecord, embedding_space: str, now: datetime) -> DriveIndexRecord:
    """The embedding model of `embedding_space` refused a chunk of the readable generation (a definite HTTP 400):
    terminal for that space, like a refused card text (§8.2; R3 F2-3). The generation goes (the caller collects every
    generation's chunks) and is not re-read: an indexed record becomes `failed` (the outcome a run's refused chunk
    reaches), a waiting or failed one keeps its content state. Its card stays; another space gives it a fresh try."""
    state, reason = rec.state, rec.reason
    if state is RecordState.INDEXED:
        state, reason = RecordState.FAILED, Reason.EXTRACT_ERROR
    return schedule(replace(rec, state=state, reason=reason, indexed_generation=None, chunk_space="",
                            chunk_reembed_pending=False, chunks_refused_space=embedding_space), now,
                    first_seen=not rec.has_card)


def access_action(enabled: bool, state: Optional[DriveSyncState], connection: DriveConnection,
                  now: datetime) -> AccessAction:
    """The tick's gate (§7.1 step 0, §7.6). Keyed on facts only, never on an exception type."""
    if not enabled:
        return AccessAction.PURGE_OPT_OUT if state is not None else AccessAction.NOOP_DISABLED
    if state is not None and state.purging:
        return AccessAction.PURGE_THEN_RESTART   # a started purge always finishes, then onboarding (§7.6)
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
    """Staleness criterion (§12.1): no clean tick, or a class-0 record overdue, for STALE_ALERT_AFTER. A gated state
    (written by an older build, awaiting the owner's rebuild) does no tick work by design and has its own alert (A5)."""
    if state.access_lost_at is not None:
        return False
    if state.applied_versions and MIGRATION_GATE_KEY not in state.applied_versions:
        return False
    since = state.last_success_at or state.created_at
    if since is None:
        return False   # no tick has run yet: no baseline to be stale against
    if now - since > STALE_ALERT_AFTER:
        return True
    return oldest_edit_due_at is not None and now - oldest_edit_due_at > STALE_ALERT_AFTER


# -- index runs (§8.1, §8.2, §7.4) ---------------------------------------------------------------------------------

def generation_started(rec: DriveIndexRecord, now: datetime) -> DriveIndexRecord:
    """Step 2 of §8.1, persisted before the first chunk write: a new generation, so a retry never reuses a crashed
    run's. The content is due again when the run would count as crashed (attempt_crashed), in the retry class."""
    return _defer(replace(rec, index_generation=rec.index_generation + 1, attempt_started_at=now,
                          chunks_refused_space=""),
                  now + timedelta(seconds=TICK_DEADLINE_S), int(Priority.RETRY), now)


def _defer(rec: DriveIndexRecord, until: datetime, priority: int, now: datetime) -> DriveIndexRecord:
    return schedule(replace(rec, defer_until=until, defer_priority=priority), now, first_seen=not rec.has_card)


def deferred(rec: DriveIndexRecord, until: datetime, now: datetime, *, maintenance: bool = False,
             priority: Optional[int] = None) -> DriveIndexRecord:
    """Hold the record's model work until `until`, still pending (§24.7, §24.8): a spent monthly budget waits for
    next_month_start in class 3 (`maintenance=True`); a model outage waits for `model_backoff_until` in the class it
    was due in. A started run is in the retry class (generation_started): its caller passes `priority`, the class
    the run started from, so a deferral never moves an edit behind first-seen work."""
    if maintenance:
        cls = int(Priority.MAINTENANCE)
    elif priority is not None:
        cls = priority
    elif rec.priority is not None:
        cls = rec.priority
    else:
        cls = int(Priority.EDIT if rec.has_card else Priority.FIRST_SEEN)
    return _defer(rec, until, cls, now)


def next_month_start(now: datetime) -> datetime:
    """The UTC month boundary a spent monthly budget waits for (§24.7)."""
    utc = now.astimezone(timezone.utc)
    year, month = (utc.year + 1, 1) if utc.month == 12 else (utc.year, utc.month + 1)
    return datetime(year, month, 1, tzinfo=timezone.utc)


def attempt_crashed(rec: DriveIndexRecord, now: datetime) -> bool:
    """A run started a whole tick deadline ago with no outcome: the instance died during it (§8.2)."""
    return (rec.attempt_started_at is not None
            and now - rec.attempt_started_at >= timedelta(seconds=TICK_DEADLINE_S))


def _settled(rec: DriveIndexRecord, now: datetime) -> DriveIndexRecord:
    """A content outcome is terminal: the quiet period and the attempt end (§7.3, §8.2); the card may be due, in
    class 0 for a record that already has one (§7.8)."""
    return schedule(replace(rec, dirty_since=None, attempts=0, attempt_started_at=None, defer_until=None,
                            defer_priority=None, model_outages=0), now, first_seen=not rec.has_card)


def indexed(rec: DriveIndexRecord, *, outcome: ExtractionOutcome, generation: int, chunk_count: int,
            now: datetime, chunk_space: str = "", keys_capped: bool = False,
            profile_fingerprint: Optional[str] = None) -> DriveIndexRecord:
    """The fence's one record write (§7.4): `generation` becomes readable for this content version, its chunks
    embedded in `chunk_space` and keyed under KEYS_VERSION (`keys_capped`: a chunk's word keys were cut). The caller
    then collects every other generation. `outcome.truncated` must include the chunker's cut (§5.4). The profile's
    issue date, else the brief's, becomes the effective date (D13); `profile_fingerprint` is the profiler input's
    fingerprint from the new text, `script_rendered` is parser@2's flag (§25.2.1). No lead text: v2 cards carry the
    profile (§25.2.7). A new content version restarts the enrichment jobs' attempts (§25.4.9)."""
    new_version = rec.indexed_version != rec.content_version
    done = replace(rec, state=RecordState.INDEXED, reason=None, indexed_generation=generation,
                   chunk_space=chunk_space, chunk_reembed_pending=False,
                   indexed_version=rec.content_version, extraction_version=EXTRACTION_VERSION,
                   extracted_by=outcome.extracted_by, text_chars=len(outcome.text), pages_total=outcome.pages_total,
                   chunk_count=chunk_count, truncated=outcome.truncated, lead_text="", brief=outcome.brief,
                   script_rendered=outcome.script_rendered, capture=outcome.capture,
                   capture_source=outcome.capture_source, pages_refused=outcome.pages_refused,
                   read_by_fallback=outcome.read_by_fallback, keys_version=KEYS_VERSION, keys_pending=False,
                   keys_capped=keys_capped, profile_input_fingerprint=profile_fingerprint,
                   effective_date=_effective(change_from_record(rec), outcome.brief, rec.effective_date or now,
                                             rec.profile, rec.kind))
    if new_version:
        done = replace(done, profile_attempts=0, passages_attempts=0)
    return _settled(done, now)


def demoted(rec: DriveIndexRecord, reason: Reason, now: datetime, *, outcome: Optional[ExtractionOutcome] = None,
            chunk_count: int = 0) -> DriveIndexRecord:
    """A deterministic content outcome (§8.2): metadata_only, no retry. The readable generation goes (the caller
    collects its chunks). `outcome` is what the extraction produced: a machine_text card keeps its lead text (§5.5),
    and a user_chunk_cap refusal keeps the would-be `chunk_count` so its release can test headroom (§5.4)."""
    if reason not in _CONTENT_REASONS:
        raise ValueError(f"{reason.value} is not a content outcome")
    text = outcome.text if outcome is not None else ""
    brief = outcome.brief if outcome is not None else None
    if outcome is not None and brief is not None and brief.doc_date is not None:   # graded before it dates the file (Δ60)
        brief = replace(brief, date_grade=brief_date_grade(brief, [outcome], rec.name))
    done = replace(rec, state=RecordState.METADATA_ONLY, reason=reason, indexed_generation=None,
                   chunk_space="", chunk_reembed_pending=False, extraction_version=EXTRACTION_VERSION,
                   extracted_by=outcome.extracted_by if outcome is not None else rec.extracted_by,
                   text_chars=len(text), pages_total=outcome.pages_total if outcome is not None else 0,
                   truncated=outcome.truncated if outcome is not None else False,
                   chunk_count=chunk_count if reason is Reason.USER_CHUNK_CAP else 0,
                   lead_text=text.strip()[:CARD_LEAD_CHARS] if reason is Reason.MACHINE_TEXT else "", brief=brief,
                   capture=outcome.capture if outcome is not None else "",   # a text that was read keeps its capture
                   capture_source=outcome.capture_source if outcome is not None else "",
                   pages_refused=outcome.pages_refused if outcome is not None else 0,
                   read_by_fallback=outcome.read_by_fallback if outcome is not None else False,
                   card_accuracy=None,   # no readable text to confirm a card against (§26.4)
                   effective_date=_effective(change_from_record(rec), brief, rec.effective_date or now,
                                             kind=rec.kind))
    return _settled(done, now)


_FAILED_REASONS = frozenset({Reason.EXTRACT_TIMEOUT, Reason.EXTRACT_MEMORY, Reason.EXTRACT_ERROR,
                             Reason.DOWNLOAD_ERROR, Reason.EMBED_TIMEOUT})
FAILED_REASONS = _FAILED_REASONS   # public: the per-file failures an owner may requeue (content_requeued)


def content_requeued(rec: DriveIndexRecord, now: datetime) -> DriveIndexRecord:
    """The owner's requeue of a `failed` content read (scripts/infrastructure/drive_content_requeue.py): a failed outcome
    stands until the content version or EXTRACTION_VERSION moves (_outcome_stands), so a fix of the failure's cause
    never reaches it. Pending again with its attempts, started attempt, quiet period, deferral and charged outages
    cleared, and `extraction_version` 0 (reextraction_due), so the outcome no longer stands and a readable generation
    of this content version does not count as indexed; due at once in class 3, like the bot's other re-reads (§12.3).
    The readable generation, the card and every other field are kept. Raises on any other record."""
    if rec.state is not RecordState.FAILED or rec.reason not in _FAILED_REASONS:
        raise ValueError(f"item {rec.item_id} is not failed with a per-file reason")
    pending = replace(reextraction_due(rec), state=RecordState.PENDING, reason=None, attempts=0,
                      attempt_started_at=None, dirty_since=None, defer_until=None, defer_priority=None,
                      model_outages=0)
    done = schedule(pending, now, first_seen=not rec.has_card)
    return done if done.due_at is None else replace(done, priority=int(Priority.MAINTENANCE))


def content_retry(rec: DriveIndexRecord, reason: Reason, now: datetime) -> DriveIndexRecord:
    """A per-file transient outcome or a crashed run (§8.2): back off in the retry class; at MAX_ATTEMPTS the record
    is `failed` with `reason` and keeps its previous readable generation, served as stale (§3.4)."""
    if reason not in _FAILED_REASONS:
        raise ValueError(f"{reason.value} is not a per-file failure")
    attempts = rec.attempts + 1
    if attempts >= MAX_ATTEMPTS:
        return _settled(replace(rec, state=RecordState.FAILED, reason=reason,
                                extraction_version=EXTRACTION_VERSION), now)
    return _defer(replace(rec, attempts=attempts, attempt_started_at=None), now + backoff(attempts),
                  int(Priority.RETRY), now)


def outage_charged(rec: DriveIndexRecord, until: datetime, now: datetime, extracted_by: str) -> DriveIndexRecord:
    """The reader failed as an outage while reading this file (final review F1-1): a 5xx it always gets, an unexpected
    adapter error, a one-page window that always times out look like an outage, and each retry re-reads (and bills)
    the windows before it. The outage is charged to the file: below MAX_ATTEMPTS it waits for the model backoff
    `until` in class 3, behind other due work; the MAX_ATTEMPTS-th is a counted per-file failure, `failed` with
    extract_error under `extracted_by` (the reader's link), so a new reader version or a new version of the file gets
    a fresh try."""
    outages = rec.model_outages + 1
    if outages >= MAX_ATTEMPTS:
        return _settled(replace(rec, state=RecordState.FAILED, reason=Reason.EXTRACT_ERROR, extracted_by=extracted_by,
                                extraction_version=EXTRACTION_VERSION), now)
    return deferred(replace(rec, model_outages=outages, attempt_started_at=None), until, now, maintenance=True)


def triaged(rec: DriveIndexRecord, result: TriageResult, now: datetime,
            photo: Optional[PhotoDescription] = None) -> DriveIndexRecord:
    """Store a verdict for this content version (§24.5). A photograph is metadata_only with its description (§25.4.3:
    `photo`, None for one the model refused or stopped on alone); text makes the content due for the reader, unless
    this version is already indexed, and clears the photo fields. The reader's work keeps the class the triage had:
    an edit stays class 0, anything else is class 2 (the tick moves upgrades to class 3)."""
    if result.item_id != rec.item_id:
        raise ValueError(f"verdict for {result.item_id} applied to {rec.item_id}")
    first_seen = rec.priority != int(Priority.EDIT)
    rec = replace(rec, triage_verdict=result.verdict, triage_version=TRIAGE_VERSION,
                  triaged_content_version=rec.content_version, attempts=0, defer_until=None, defer_priority=None)
    if result.verdict is TriageVerdict.PHOTO:
        done = replace(rec, state=RecordState.METADATA_ONLY, reason=Reason.PHOTOGRAPH, caption="",
                       indexed_generation=None, chunk_space="", chunk_reembed_pending=False, lead_text="", brief=None,
                       dirty_since=None, capture="", capture_source="",   # a photograph has none (Δ57)
                       card_accuracy=None, accuracy_version=0,            # nor a score (§26.4)
                       effective_date=_effective(change_from_record(rec), None, rec.effective_date or now,
                                             kind=rec.kind))
        done = _without_photo(done) if photo is None else replace(
            done, photo_description=photo.description, photo_objects=tuple(photo.objects), photo_scene=photo.scene,
            photo_scene_labels=photo.scene_labels, visible_text=photo.visible_text)
        return schedule(done, now, first_seen=first_seen)
    state = RecordState.INDEXED if _indexed_now(rec) else RecordState.PENDING
    # §26.3 rule 1: the provider's camera facet decides; else what triage said (kept until the reader's own answer)
    # the reader's stored word (an already indexed image) outranks triage's, as in file_capture
    model = (model_capture(rec.capture) if state is RecordState.INDEXED else None) or model_capture(result.capture)
    capture, source = settle(camera_capture(rec.facets), model)
    # a capture that changed on an indexed image changes its source share: the score is stale, and `accuracy_version` 0
    # sends it to the upgrade pass, which re-scores it from the stored profile and chunks (`scored`, B-minor 4)
    stale = state is RecordState.INDEXED and (capture, source) != (rec.capture, rec.capture_source)
    return schedule(_without_photo(replace(rec, state=state, reason=None, caption="", capture=capture,
                                           capture_source=source,
                                           accuracy_version=0 if stale else rec.accuracy_version)),
                    now, first_seen=first_seen)


def can_start_file(elapsed_s: float, *, routed: bool) -> bool:
    """§24.3 / §7.1: a file starts only if its worst case still ends before the tick deadline. `routed`: it may
    go to the reader (a PDF or an information-bearing image)."""
    worst = (DOWNLOAD_TIMEOUT_S + EXTRACT_WALL_TIMEOUT_S + (MODEL_FILE_BUDGET_S if routed else 0)
             + EMBED_FILE_BUDGET_S + FILE_START_MARGIN_S)
    return elapsed_s + worst < TICK_DEADLINE_S


def can_start_reembed(elapsed_s: float) -> bool:
    """A re-embed waits twice (chunks, then hints), each under EMBED_FILE_BUDGET_S, and downloads nothing."""
    return elapsed_s + 2 * EMBED_FILE_BUDGET_S + FILE_START_MARGIN_S < TICK_DEADLINE_S


# -- model usage and outages on the sync state (§24.7, §24.8) -------------------------------------------------------

def month_key(now: datetime) -> str:
    """The calendar month (UTC) monthly budgets are counted in."""
    return now.astimezone(timezone.utc).strftime("%Y-%m")


def usage_now(state: DriveSyncState, now: datetime) -> Tuple[int, int]:
    """(pages sent to the reader, images sent to triage) this month; a new month starts at zero."""
    if state.usage_month != month_key(now):
        return 0, 0
    return state.model_pages_used, state.triage_images_used


def with_usage(state: DriveSyncState, now: datetime, *, pages: int = 0, images: int = 0) -> DriveSyncState:
    used_pages, used_images = usage_now(state, now)
    return replace(state, usage_month=month_key(now), model_pages_used=used_pages + pages,
                   triage_images_used=used_images + images)


def model_available(state: DriveSyncState, now: datetime) -> bool:
    return state.model_backoff_until is None or now >= state.model_backoff_until


def model_outage(state: DriveSyncState, now: datetime) -> DriveSyncState:
    """A model outage defers model work only (§24.8), with the §7.7 doubling backoff."""
    failures = state.model_failures + 1
    delay = min(MODEL_BACKOFF_BASE_S * 2 ** (failures - 1), MODEL_BACKOFF_CAP_S)
    return replace(state, model_failures=failures, model_backoff_until=now + timedelta(seconds=delay))


def model_recovered(state: DriveSyncState) -> DriveSyncState:
    return replace(state, model_failures=0, model_backoff_until=None)


# -- files the bot cannot open (§26.2, D33, Δ47-Δ52) ----------------------------------------------------------------

def access_recheck_at(rec: DriveIndexRecord) -> Optional[datetime]:
    """When a marked record is read again by id (Δ51, OQ-C1): INACCESSIBLE_RETRY_MIN after the last read once delta or a
    listing saw the item or its parent since, else INACCESSIBLE_RETRY_SLOW after it; None for a readable record."""
    checked = rec.inaccessible_checked_at or rec.inaccessible_since
    if not inaccessible(rec) or checked is None:
        return None
    return checked + (INACCESSIBLE_RETRY_MIN if rec.inaccessible_reobserved else INACCESSIBLE_RETRY_SLOW)


def access_recheck_due(rec: DriveIndexRecord, now: datetime) -> bool:
    at = access_recheck_at(rec)
    return at is not None and at <= now


def marked_inaccessible(rec: DriveIndexRecord, now: datetime) -> DriveIndexRecord:
    """An id-addressed read of an item delta lists answered "not found" (§26.2, Δ48): the bot has no access. A started
    run ends without an attempt (no content_retry, no `failed`); the content outcome, the card and a readable generation
    stay (Δ47, Δ52). A record already marked keeps its `inaccessible_since`, so the user is told once. A pending
    re-listing restarts from its first page once the folder can be read again."""
    return schedule(replace(rec, inaccessible_since=rec.inaccessible_since or now, inaccessible_checked_at=now,
                            inaccessible_reobserved=False, attempt_started_at=None, defer_until=None,
                            defer_priority=None, reobserve_cursor=None), now, first_seen=not rec.has_card)


def access_rechecked(rec: DriveIndexRecord, now: datetime) -> DriveIndexRecord:
    """The re-read answered "not found" again: the retry clock restarts, the observation is spent."""
    return schedule(replace(rec, inaccessible_checked_at=now, inaccessible_reobserved=False), now,
                    first_seen=not rec.has_card)


def access_observed(rec: DriveIndexRecord, now: datetime) -> DriveIndexRecord:
    """Delta or a listing saw the marked item or its parent folder: one read is due INACCESSIBLE_RETRY_MIN after the
    last (Δ51)."""
    return schedule(replace(rec, inaccessible_reobserved=True), now, first_seen=not rec.has_card)


def access_regained(rec: DriveIndexRecord, now: datetime) -> DriveIndexRecord:
    """The item is readable again: the tick's fields clear silently and the ordinary content path resumes. The fan-out's
    `inaccessible_notified_since` stays (only the fan-out writes it, Δ54): a later loss of access is a new
    `inaccessible_since`, so it is notified again."""
    return schedule(replace(rec, inaccessible_since=None, inaccessible_checked_at=None, inaccessible_reobserved=False),
                    now, first_seen=not rec.has_card)

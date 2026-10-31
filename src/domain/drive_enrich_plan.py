"""
Drive enrichment planning — the profile and passage-hint jobs, the keys work, the derived queue fields and the
per-agent model backoff of Search v2 (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §25.4.5-§25.4.9). Pure: no I/O, no
clock. The tick runs `planned(rec, ctx)` on every record it writes (`_commit`), so every write leaves consistent job
states, `about_keys` and queue fields, whatever step wrote it.

Public interface:
    PROFILER_AGENT, PASSAGES_AGENT                       agent types (the per-agent backoff map's keys)
    enrich_context(now, usage, build) -> EnrichContext;  default_context(now) -> EnrichContext
    available_usd(ctx, cls) -> float                     the open initial build + what the month's class cap has left
    profile_estimate(rec) / passages_estimate(rec) -> float      one call at the primary's price
    effective_from(rec, profile, *, now=None) -> Optional[datetime]   the profile's issue date first (D13 extended)
    template_reason(rec) -> Optional[TemplateReason];  chunks_to_hint(rec) -> int
    input_fingerprint_of(rec, chunks) -> Optional[str]  `profile_input_fingerprint` from stored text
    profile_plan(rec, ctx) / passages_plan(rec, ctx) / keys_planned(rec, now) / derived(rec) / planned(rec, ctx)
    described(rec) / describe_gap(rec) -> str;  NO_PASSAGE_KINDS, COVERAGE_COUNTERS   the coverage line's counts
                                                         (§25.5.6, XC-3, XC-9)
    profile_settled(rec) / passages_settled(rec) / keys_settled(rec)     the upgrade pass's per-key criteria
    profiled(rec, profile, *, source, fingerprint, now) / reverified(rec, profile, now) / regraded(rec, profile, now)
    profile_charged(rec, status, now) / profile_unusable(rec) / passages_unusable(rec)
    passages_started(rec) / passages_advanced(rec, next_index, *, finished, now) / passages_charged(rec)
    agent_available(backoffs, agent, now) / agent_outage(backoffs, agent, now) / agent_recovered(backoffs, agent)
    similarity(a, b) -> float                            cosine similarity (grounding rule 4)

The state table (§25.4.5), per job, first match wins:
    none      the policy gives the kind no such job, or nothing readable (no readable generation; a folder without a
              README excerpt). The job's fields are cleared (§25.4.9: a write that drops the readable generation).
    current   nothing is due.
    held      a hold recorded for the current version and fingerprint (passages: generation).
    deferred  the refresh cooldown is not over (`*_defer_until`), or the record's content run is due (an edit, a
              re-extraction): its readable text is about to be replaced, so no call is made on it and no cooldown
              starts from it; the content run's commit re-plans the job (review rv_sv2_p5 MINOR 1). A model backoff
              is not a state (§25.4.6). Also while the bot cannot open the file (§26.2): no job starts, the deferral
              has no wake time, the job counts as settled for the upgrade pass and the file is no describe gap (the
              coverage line counts it as one the bot cannot open, Δ56); access regained re-plans it.
    parked    one call no longer fits the open initial build plus the month's rest of the class cap (§25.4.5:
              "a file starts only if its expected cost at the primary's price fits"); wakes when the month turns.
    ready     work is due.
Templates (§25.3) never cost a call, so they are never parked: machine text and script-rendered pages get theirs here
at once; a short note's needs its text, so its job is `ready` and the tick writes it from the stored text.
"""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Mapping, Optional, Sequence

from .drive_card import effective_date
from .drive_enrichment import (
    USD_CAPS, DriveProfile, EnrichContext, HoldReason, InitialBuild, JobState, ModelUsageMonth, ProfileSource,
    UsageClass,
)
from .drive_enrichment_calls import ModelReplyStatus, ProfileInput
from .drive_index_policy import TRANSCRIBED_KINDS
from .drive_index import (
    KEYS_VERSION, PASSAGES_VERSION, PROFILE_VERSION, DriveChange, DriveChunk, DriveIndexRecord, Priority, Reason,
    RecordState, Scope, inaccessible,
)
from .drive_index_planner import content_due
from .drive_index_params import (
    INITIAL_BUILD_USD, MAX_ATTEMPTS, MODEL_BACKOFF_BASE_S, MODEL_BACKOFF_CAP_S, PASSAGE_CHUNK_USD_ESTIMATE,
    PASSAGE_CHUNKS_PER_CALL, PASSAGE_MAX_CHUNKS_PER_FILE, PASSAGE_REFRESH_COOLDOWN, PROFILE_REFRESH_COOLDOWN,
    PROFILE_USD_ESTIMATE,
)
from .drive_keys import about_keys_capped
from .drive_profile import (
    KindPolicy, ProfileOutput, TemplateReason, _HasText, brief_date_grade, input_fingerprint, issued_of,
    profile_input, record_policy, stored_text, template_profile, valid_until_of,
)

PROFILER_AGENT = "drive_profiler"
PASSAGES_AGENT = "drive_passages"
_SINCE = ":since"            # the backoff map's streak start, beside the agent's own `until`

_R, _C, _D, _P, _H, _N = (JobState.READY.value, JobState.CURRENT.value, JobState.DEFERRED.value,
                          JobState.PARKED.value, JobState.HELD.value, JobState.NONE.value)
# Text kinds with a profile but no doc2query by policy (§25.3): books, sheets and data files
NO_PASSAGE_KINDS = frozenset({"epub", "xlsx", "xls", "ods", "csv", "json"})
GAP_WAITING, GAP_HELD, GAP_NO_PASSAGES = "waiting", "held", "no_passages"
# The sync-state counters the tick stores them under; search reads them (XC-9: no count query per search)
COVERAGE_COUNTERS: Mapping[str, str] = {GAP_WAITING: "coverage_waiting", GAP_HELD: "coverage_held",
                                        GAP_NO_PASSAGES: "coverage_no_passages"}


# -- budget -------------------------------------------------------------------------------------------------------

def enrich_context(now: datetime, usage: ModelUsageMonth, build: InitialBuild) -> EnrichContext:
    """What the plans decide with this tick: the month's dollars per class against its cap — the dollars the initial
    build paid left out, since the build is spent first, before the caps (§25.4.5, D27; review rv_sv2_p5 M1) — the
    caps and the initial build."""
    month = {c.value: usage.cap_usd(c.value) for c in UsageClass}
    return EnrichContext(now=now, month_usd=month, caps_usd=dict(USD_CAPS),
                         initial_balance_usd=max(0.0, build.allowance_usd - build.spent_usd),
                         initial_open=build.closed_at is None)


def default_context(now: datetime) -> EnrichContext:
    """No usage read (a write outside a tick): nothing is parked."""
    return EnrichContext(now=now, month_usd={}, caps_usd=dict(USD_CAPS), initial_balance_usd=INITIAL_BUILD_USD,
                         initial_open=True)


def available_usd(ctx: EnrichContext, cls: str) -> float:
    # G8: the initial build never pays for transcription (§27.10: no initial-build allowance)
    initial = ctx.initial_balance_usd if ctx.initial_open and cls != UsageClass.TRANSCRIBE.value else 0.0
    return max(0.0, initial) + max(0.0, ctx.caps_usd.get(cls, 0.0) - ctx.month_usd.get(cls, 0.0))


def profile_estimate(rec: DriveIndexRecord) -> float:
    return PROFILE_USD_ESTIMATE


def passages_estimate(rec: DriveIndexRecord) -> float:
    """One passage call: the next PASSAGE_CHUNKS_PER_CALL chunks (the tick checks again before every batch)."""
    left = max(0, chunks_to_hint(rec) - (rec.passages_next_index if rec.passages_generation == rec.indexed_generation
                                         else 0))
    return PASSAGE_CHUNK_USD_ESTIMATE * max(1, min(PASSAGE_CHUNKS_PER_CALL, left))


# -- dates --------------------------------------------------------------------------------------------------------

def _utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def effective_from(rec: DriveIndexRecord, profile: Optional[DriveProfile], *,
                   now: Optional[datetime] = None) -> Optional[datetime]:
    """§25.4.1 (D13 extended): the verified issue date of the profile, else the reader's brief date, else §3.1 (taken,
    filesystem-modified, server time)."""
    # a date said in a recording never re-dates it (§27.8 a, G38): media keep §3.1
    own_date = rec.kind not in TRANSCRIBED_KINDS
    issued = issued_of(profile) if profile is not None and own_date else None
    if issued is not None:
        return _utc(issued)
    if own_date and rec.brief is not None and rec.brief.held_date is not None:
        return rec.brief.held_date
    change = DriveChange(item_id=rec.item_id, media=rec.media, fs_modified_at=rec.fs_modified_at,
                         modified_at=rec.modified_at)
    fallback = rec.effective_date or now
    if fallback is None and change.fs_modified_at is None and change.modified_at is None:
        return None
    return effective_date(change, fallback or datetime.min.replace(tzinfo=timezone.utc))


# -- policy --------------------------------------------------------------------------------------------------------

def _policy(rec: DriveIndexRecord) -> KindPolicy:
    return record_policy(rec, text_chars=rec.text_chars, script_rendered=rec.script_rendered)


def template_reason(rec: DriveIndexRecord) -> Optional[TemplateReason]:
    """Why the record's profile is a template (§25.3), or None."""
    if _policy(rec).output is not ProfileOutput.TEMPLATE:
        return None
    if rec.reason is Reason.MACHINE_TEXT:
        return TemplateReason.MACHINE_TEXT
    if rec.script_rendered:
        return TemplateReason.SCRIPT_RENDERED
    return TemplateReason.SHORT


def chunks_to_hint(rec: DriveIndexRecord) -> int:
    return min(max(0, rec.chunk_count), PASSAGE_MAX_CHUNKS_PER_FILE)


def input_fingerprint_of(rec: DriveIndexRecord, chunks: Sequence[DriveChunk]) -> Optional[str]:
    """`profile_input_fingerprint` (§25.2.1) from the stored text (or a folder's README excerpt): the model input's
    fingerprint, a short note's text for its template, None when nothing is sent (no policy, a fixed template)."""
    reason = template_reason(rec)
    if reason is TemplateReason.SHORT:
        text = stored_text(chunks).strip()
        return input_fingerprint(rec.kind, ProfileInput(head=text)) if text else None
    if reason is not None:
        return None
    inp = profile_input(rec, chunks, rec.kind)
    return input_fingerprint(rec.kind, inp) if inp is not None else None


def _source_available(rec: DriveIndexRecord, output: ProfileOutput, reason: Optional[TemplateReason]) -> bool:
    if output is ProfileOutput.NONE or rec.state is RecordState.DELETING:
        return False
    if rec.is_folder:
        return bool(rec.readme_excerpt) and rec.scope is Scope.OPAQUE_ROOT
    if reason is TemplateReason.MACHINE_TEXT:
        return True                          # its text is the lead text; the template needs none
    return rec.indexed_generation is not None


# -- profile_plan --------------------------------------------------------------------------------------------------

def profile_hold_key(rec: DriveIndexRecord) -> str:
    return f"{PROFILE_VERSION}:{rec.profile_input_fingerprint or ''}"


def _profile_cleared(rec: DriveIndexRecord, now: datetime) -> DriveIndexRecord:
    """The profile and its job's fields cleared; the refresh cooldown (`profile_defer_until`) is kept, so a file
    whose readable text drops and comes back is not profiled again within it (XC-6)."""
    had_profile = rec.profile is not None
    out = replace(rec, profile=None, valid_until=None, profile_source=None, profile_fingerprint=None,
                  profile_version=0, profiled_content_version="", profile_state=_N,
                  profile_parked_month="", profile_hold=None, profile_hold_version="", profile_attempts=0,
                  profile_input_fingerprint=None if not rec.is_folder else rec.profile_input_fingerprint)
    if had_profile:
        # the score read that profile's grades: it is stale with it; the write that gives the record a profile or a
        # brief scores it again, and until then the upgrade pass sees it unscored (§26.4)
        out = replace(out, effective_date=effective_from(out, None, now=now) or rec.effective_date,
                      card_accuracy=None, accuracy_version=0)
    return out


def profile_plan(rec: DriveIndexRecord, ctx: EnrichContext) -> DriveIndexRecord:
    policy = _policy(rec)
    reason = template_reason(rec)
    if not _source_available(rec, policy.output, reason):
        return _profile_cleared(rec, ctx.now) if (rec.profile is not None or rec.profile_state != _N
                                                  or rec.profile_attempts or rec.profile_hold) else rec
    if rec.is_folder:
        inp = profile_input(rec, (), "folder")
        rec = replace(rec, profile_input_fingerprint=input_fingerprint("folder", inp) if inp is not None else None)
    if reason in (TemplateReason.MACHINE_TEXT, TemplateReason.SCRIPT_RENDERED):
        if not (rec.profile_source == ProfileSource.TEMPLATE.value and rec.profile_version == PROFILE_VERSION
                and rec.profile is not None):
            rec = profiled(rec, template_profile(reason, rec.kind, ""), source=ProfileSource.TEMPLATE,
                           fingerprint=rec.profile_input_fingerprint, now=ctx.now)
        return replace(rec, profile_state=_C)
    template = reason is TemplateReason.SHORT
    wanted = ProfileSource.TEMPLATE.value if template else ProfileSource.MODEL.value
    due = (rec.profile is None or rec.profile_source != wanted or rec.profile_version != PROFILE_VERSION
           or rec.profile_fingerprint != rec.profile_input_fingerprint)
    if not due:
        return replace(rec, profile_state=_C, profile_parked_month="")
    key = profile_hold_key(rec)
    if rec.profile_hold is not None and rec.profile_hold_version == key:
        return replace(rec, profile_state=_H)
    if content_due(rec):
        return replace(rec, profile_state=_D)   # the text is about to change: profiled after its content run
    if inaccessible(rec):
        return replace(rec, profile_state=_D, profile_parked_month="")   # no job while closed (§26.2)
    bump = rec.profile is not None and rec.profile_version != PROFILE_VERSION
    if (not template and not bump and rec.profile_defer_until is not None and ctx.now < rec.profile_defer_until):
        return replace(rec, profile_state=_D)
    if not template and available_usd(ctx, UsageClass.PROFILE.value) < profile_estimate(rec):
        return replace(rec, profile_state=_P, profile_parked_month=_month(ctx.now), profile_hold_version=key)
    return replace(rec, profile_state=_R, profile_parked_month="")


def profiled(rec: DriveIndexRecord, profile: DriveProfile, *, source: ProfileSource, fingerprint: Optional[str],
             now: datetime) -> DriveIndexRecord:
    """A profile written (verified, or a template): its version, fingerprint and content version, `valid_until`
    (00:00 UTC of the printed date) and the effective date; a model profile starts the refresh cooldown (§25.4.7), a
    template keeps the last model profile's (a note whose edits cross PROFILE_MIN_CHARS back and forth is profiled at
    most once per cooldown, XC-6)."""
    valid = valid_until_of(profile)
    model = source is ProfileSource.MODEL
    # the input it was made from IS the current input (the derive step's fingerprint, §25.2.1): both are written
    out = replace(rec, profile=profile, profile_source=source.value, profile_fingerprint=fingerprint,
                  profile_input_fingerprint=fingerprint if fingerprint is not None else rec.profile_input_fingerprint,
                  profile_version=PROFILE_VERSION, profiled_content_version=rec.content_version,
                  valid_until=_utc(valid) if valid is not None else None,
                  profile_defer_until=now + PROFILE_REFRESH_COOLDOWN if model else rec.profile_defer_until,
                  profile_hold=None, profile_hold_version="", profile_attempts=0, profile_parked_month="")
    return replace(out, effective_date=effective_from(out, profile, now=now) or rec.effective_date)


def reverified(rec: DriveIndexRecord, profile: DriveProfile, now: datetime) -> DriveIndexRecord:
    """The stored profile re-verified against a new text (§25.4.7): its fields and dates follow; its fingerprint and
    cooldown stay, so the plan decides whether a new profile is due."""
    valid = valid_until_of(profile)
    out = replace(rec, profile=profile, valid_until=_utc(valid) if valid is not None else None,
                  profiled_content_version=rec.content_version)
    return replace(out, effective_date=effective_from(out, profile, now=now) or rec.effective_date)


def regraded(rec: DriveIndexRecord, profile: DriveProfile, now: datetime) -> DriveIndexRecord:
    """The stored profile graded again by the accuracy upgrade (§26.4 "Recomputing"): its grades and the dates that
    follow them. Nothing else about the profile job moves — its version, fingerprint, content version and cooldown
    stay, so a profile that was already due stays due."""
    valid = valid_until_of(profile)
    out = replace(rec, profile=profile, valid_until=_utc(valid) if valid is not None else None)
    return replace(out, effective_date=effective_from(out, profile, now=now) or rec.effective_date)


def brief_graded(rec: DriveIndexRecord, chunks: Sequence[_HasText], now: datetime) -> DriveIndexRecord:
    """The reader brief's date graded against the stored text (§26.4, Δ60), its grade kept on the brief and the effective
    date following it: a date the text does not hold never becomes the file's date. No brief date, no change."""
    if rec.brief is None or rec.brief.doc_date is None:
        return rec
    out = replace(rec, brief=replace(rec.brief, date_grade=brief_date_grade(rec.brief, chunks, rec.name)))
    return replace(out, effective_date=effective_from(out, out.profile, now=now) or rec.effective_date)


def profile_charged(rec: DriveIndexRecord, status: ModelReplyStatus, now: datetime) -> DriveIndexRecord:
    """§25.4.6 for one profile call that did not answer: a refusal by the primary holds `refused`; still stopped at the
    cap after halving holds `failed` (§25.4.4); `failed` and `invalid` count an attempt and hold `failed` at
    MAX_ATTEMPTS; `unavailable` charges nothing. Holds are for the current version and fingerprint."""
    key = profile_hold_key(rec)
    if status is ModelReplyStatus.REFUSED:
        return replace(rec, profile_hold=HoldReason.REFUSED.value, profile_hold_version=key)
    if status is ModelReplyStatus.STOPPED_AT_CAP:
        return replace(rec, profile_hold=HoldReason.FAILED.value, profile_hold_version=key,
                       profile_attempts=rec.profile_attempts + 1)
    if status in (ModelReplyStatus.FAILED, ModelReplyStatus.INVALID):
        attempts = rec.profile_attempts + 1
        if attempts >= MAX_ATTEMPTS:
            return replace(rec, profile_attempts=attempts, profile_hold=HoldReason.FAILED.value,
                           profile_hold_version=key)
        return replace(rec, profile_attempts=attempts)
    return rec


def profile_unusable(rec: DriveIndexRecord) -> DriveIndexRecord:
    """A ready profile job the stored text cannot serve (no text for a short note's template, no profiler input): held
    `failed` for the current version and fingerprint, so a run that can write nothing never leaves it `ready` (review
    rv_sv2_p5 M4). A new text — a new fingerprint — or a version bump makes it ready again."""
    return replace(rec, profile_hold=HoldReason.FAILED.value, profile_hold_version=profile_hold_key(rec))


# -- passages_plan -------------------------------------------------------------------------------------------------

def passages_hold_key(rec: DriveIndexRecord) -> str:
    return f"{PASSAGES_VERSION}:{rec.indexed_generation}"


def _passages_cleared(rec: DriveIndexRecord) -> DriveIndexRecord:
    """The passage job's fields cleared, but when it last finished and under which version are kept: the refresh
    cooldown runs from there, so a generation that drops and comes back is not hinted again within it (XC-6)."""
    return replace(rec, passages_state=_N, passages_defer_until=None, passages_parked_month="", passages_hold=None,
                   passages_hold_version="", passages_attempts=0, passages_generation=None, passages_next_index=0)


def _passages_done(rec: DriveIndexRecord) -> bool:
    return (rec.passages_version == PASSAGES_VERSION and rec.passages_generation == rec.indexed_generation
            and rec.passages_next_index >= chunks_to_hint(rec))


def passages_plan(rec: DriveIndexRecord, ctx: EnrichContext) -> DriveIndexRecord:
    policy = _policy(rec)
    if (not policy.doc2query or rec.indexed_generation is None or chunks_to_hint(rec) == 0
            or rec.state is RecordState.DELETING):
        empty = (rec.passages_state == _N and rec.passages_generation is None and not rec.passages_attempts
                 and rec.passages_hold is None and rec.passages_next_index == 0 and rec.passages_defer_until is None
                 and not rec.passages_parked_month and not rec.passages_hold_version)
        return rec if empty else _passages_cleared(rec)
    if _passages_done(rec):
        return replace(rec, passages_state=_C, passages_parked_month="", passages_defer_until=None)
    key = passages_hold_key(rec)
    if rec.passages_hold is not None and rec.passages_hold_version == key:
        return replace(rec, passages_state=_H)
    if content_due(rec) or inaccessible(rec):   # hinted after its content run; never while closed (§26.2)
        return replace(rec, passages_state=_D, passages_defer_until=None, passages_parked_month="")
    if (rec.passages_version == PASSAGES_VERSION and rec.passages_done_at is not None
            and ctx.now < rec.passages_done_at + PASSAGE_REFRESH_COOLDOWN):
        return replace(rec, passages_state=_D, passages_defer_until=rec.passages_done_at + PASSAGE_REFRESH_COOLDOWN)
    if available_usd(ctx, UsageClass.PASSAGE.value) < passages_estimate(rec):
        return replace(rec, passages_state=_P, passages_parked_month=_month(ctx.now), passages_hold_version=key,
                       passages_defer_until=None)
    return replace(rec, passages_state=_R, passages_parked_month="", passages_defer_until=None)


def passages_started(rec: DriveIndexRecord) -> DriveIndexRecord:
    """A passage run begins on the readable generation: a new generation or version starts from its first chunk."""
    if rec.passages_generation == rec.indexed_generation and rec.passages_version in (0, PASSAGES_VERSION):
        return replace(rec, passages_version=PASSAGES_VERSION)
    return replace(rec, passages_generation=rec.indexed_generation, passages_next_index=0,
                   passages_version=PASSAGES_VERSION, passages_attempts=0)


def passages_advanced(rec: DriveIndexRecord, next_index: int, *, finished: bool, now: datetime) -> DriveIndexRecord:
    """Progress saved per batch (§25.4.5): the next chunk to send; a finished run records when (the cooldown)."""
    if finished:
        return replace(rec, passages_next_index=max(next_index, chunks_to_hint(rec)), passages_done_at=now,
                       passages_attempts=0, passages_version=PASSAGES_VERSION)
    return replace(rec, passages_next_index=next_index)


def passages_charged(rec: DriveIndexRecord) -> DriveIndexRecord:
    """A charged passage call (failed, invalid): at MAX_ATTEMPTS the job is held `failed` for this generation."""
    attempts = rec.passages_attempts + 1
    if attempts >= MAX_ATTEMPTS:
        return replace(rec, passages_attempts=attempts, passages_hold=HoldReason.FAILED.value,
                       passages_hold_version=passages_hold_key(rec))
    return replace(rec, passages_attempts=attempts)


def passages_unusable(rec: DriveIndexRecord) -> DriveIndexRecord:
    """A ready passage job whose readable generation has no stored chunk to send: held `failed` for this generation
    (review rv_sv2_p5 M4); a new generation makes it ready again."""
    return replace(rec, passages_hold=HoldReason.FAILED.value, passages_hold_version=passages_hold_key(rec))


# -- keys, derived, the whole plan ---------------------------------------------------------------------------------

def keys_planned(rec: DriveIndexRecord, now: datetime) -> DriveIndexRecord:
    """§25.4.5 keys: due when the readable generation's keys were made under another KEYS_VERSION — class 3 work in the
    content loop, never budgeted. Without a readable generation only the record's own keys exist, and `planned`
    recomputes them on every write: stamped at once."""
    if rec.indexed_generation is None:
        if rec.keys_pending or rec.keys_version != KEYS_VERSION:
            return replace(rec, keys_pending=False, keys_version=KEYS_VERSION)
        return rec
    pending = rec.keys_version != KEYS_VERSION
    if pending and (rec.due_at is None or rec.due_at > now):
        # due now even when the record waits for something else (next month's reader budget, a quiet period, a
        # backoff): those waits live in `defer_until` and the quiet period, which the content work still honours, so
        # a keys bump never waits a month behind them (review rv_sv2_p5 MINOR 9)
        cls = min(rec.priority, int(Priority.MAINTENANCE)) if rec.priority is not None else int(Priority.MAINTENANCE)
        return replace(rec, keys_pending=True, due_at=now, priority=cls)
    return replace(rec, keys_pending=pending) if pending != rec.keys_pending else rec


def _month(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m")


def _next_month(now: datetime) -> datetime:
    utc = now.astimezone(timezone.utc)
    year, month = (utc.year + 1, 1) if utc.month == 12 else (utc.year, utc.month + 1)
    return datetime(year, month, 1, tzinfo=timezone.utc)


def described(rec: DriveIndexRecord) -> bool:
    """The file has a description search can use (XC-3): a profile, and — when the policy gives it passage hints —
    a passage run that finished at least once (on any generation)."""
    return rec.profile is not None and (rec.passages_state == _N or rec.passages_done_at is not None)


def describe_gap(rec: DriveIndexRecord) -> str:
    """What the coverage line counts the file as (§25.5.6): a text file with no description yet whose job waits
    (`waiting`: ready, deferred, parked) or is held (`held`), or a profiled book, sheet or data file (`no_passages`).
    A file that is already described is not "not described" while its re-description waits (XC-3). Never a folder."""
    if rec.is_folder or inaccessible(rec):   # a closed file is counted as one the bot cannot open (§26.2, Δ56)
        return ""
    jobs = (rec.profile_state, rec.passages_state)
    if not described(rec):
        if any(j in (_R, _D, _P) for j in jobs):
            return GAP_WAITING
        if _H in jobs:
            return GAP_HELD
    if rec.kind in NO_PASSAGE_KINDS and rec.profile_state == _C:
        return GAP_NO_PASSAGES
    return ""


def derived(rec: DriveIndexRecord, now: datetime) -> DriveIndexRecord:
    """`enrich_ready`, `enrich_wake_at` (a deferred job's time; a parked job's month turn), `effective_day` and
    `describe_gap`."""
    # a deferral for a due content run has no wake time of its own (its commit re-plans): a past cooldown end is none
    wakes = [at for state, at in ((rec.profile_state, rec.profile_defer_until),
                                  (rec.passages_state, rec.passages_defer_until))
             if state == _D and at is not None and at > now]
    if _P in (rec.profile_state, rec.passages_state):
        wakes.append(_next_month(now))
    day = rec.effective_date.astimezone(timezone.utc).date().isoformat() if rec.effective_date is not None else ""
    return replace(rec, enrich_ready=_R in (rec.profile_state, rec.passages_state),
                   enrich_wake_at=min(wakes) if wakes else None, effective_day=day, describe_gap=describe_gap(rec))


def planned(rec: DriveIndexRecord, ctx: EnrichContext) -> DriveIndexRecord:
    """Every record write's plan (§25.4.9): both jobs, the keys work, `about_keys` (only ever computed here, by
    `about_keys_of`) and the derived queue fields. Idempotent."""
    rec = profile_plan(rec, ctx)
    rec = passages_plan(rec, ctx)
    rec = keys_planned(rec, ctx.now)
    keys, capped = about_keys_capped(rec)
    rec = replace(rec, about_keys=keys, keys_capped=rec.keys_capped or capped)
    return derived(rec, ctx.now)


# -- settled per version key (the upgrade pass, §25.4.5) -----------------------------------------------------------

def profile_settled(rec: DriveIndexRecord, ctx: EnrichContext) -> bool:
    """The profile job, as planned now, is current or none, or held/parked for the current version and fingerprint
    (§25.4.5): the upgrade pass marks `profile` applied only when every record is settled."""
    fresh = profile_plan(rec, ctx)
    if inaccessible(fresh):
        return True   # waits for access, like a hold: it never keeps a version from applying (§26.2)
    return fresh.profile_state in (_C, _N) or (fresh.profile_state in (_H, _P)
                                               and fresh.profile_hold_version == profile_hold_key(fresh))


def passages_settled(rec: DriveIndexRecord, ctx: EnrichContext) -> bool:
    """The same for the passage-hint job (held/parked for the current version and generation)."""
    fresh = passages_plan(rec, ctx)
    if inaccessible(fresh):
        return True
    return fresh.passages_state in (_C, _N) or (fresh.passages_state in (_H, _P)
                                                and fresh.passages_hold_version == passages_hold_key(fresh))


def keys_settled(rec: DriveIndexRecord) -> bool:
    """Every key of the record and its readable chunks is made under KEYS_VERSION."""
    return rec.indexed_generation is None or (rec.keys_version == KEYS_VERSION and not rec.keys_pending)


# -- per-agent model backoff (§25.4.6) -----------------------------------------------------------------------------

def agent_available(backoffs: Mapping[str, datetime], agent: str, now: datetime) -> bool:
    until = backoffs.get(agent)
    return until is None or now >= until


def agent_outage(backoffs: Mapping[str, datetime], agent: str, now: datetime) -> Dict[str, datetime]:
    """An outage of one agent type: its own backoff, doubling over a streak (§7.7) — the delay is how long the streak
    has lasted, between MODEL_BACKOFF_BASE_S and MODEL_BACKOFF_CAP_S. A streak ends on success, or when the next
    outage comes more than MODEL_BACKOFF_CAP_S after the last backoff ended."""
    out = dict(backoffs)
    until = backoffs.get(agent)
    since = backoffs.get(agent + _SINCE)
    if since is None or until is None or now > until + timedelta(seconds=MODEL_BACKOFF_CAP_S):
        since = now
    delay = min(max((now - since).total_seconds(), MODEL_BACKOFF_BASE_S), MODEL_BACKOFF_CAP_S)
    out[agent] = now + timedelta(seconds=delay)
    out[agent + _SINCE] = since
    return out


def agent_recovered(backoffs: Mapping[str, datetime], agent: str) -> Dict[str, datetime]:
    return {k: v for k, v in backoffs.items() if k not in (agent, agent + _SINCE)}


def similarity(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 0.0 if na == 0 or nb == 0 else dot / (na * nb)

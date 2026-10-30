"""The transcript job state machine and the budget decisions (RFC §27.3, §27.5, §27.10; G8, G9, G12, G21, G37, G39)."""
import inspect
import itertools
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.domain import drive_transcript_plan as pm
from src.domain.drive_content_identity import ContentIdentity
from src.domain.drive_index import TRANSCRIBE_VERSION, DriveIndexRecord, Reason, RecordState, Scope
from src.domain.drive_index_params import (
    MAX_ATTEMPTS, QUIET, TRANSCRIBE_HOUR_USD_ESTIMATE, TRANSCRIBE_MAX_BYTES, TRANSCRIBE_MAX_SECONDS,
    TRANSCRIBE_REREAD_COOLDOWN, TRANSCRIBE_USD_PER_MONTH,
)
from src.domain.drive_transcript import JobWindow, MediaProbe, TranscriptJob, TranscriptStub, WindowSpec
from src.domain.drive_transcript_plan import (
    StartOutcome, TranscriptContext, WindowAction, WindowCheck, charge_attempt, effective_monthly_cap, estimate_usd,
    file_estimate_usd, fits, mark_current, park, queue_blocked, release, resume_window, reserve_if_fits, reset_on_new_version,
    start_file, state_for_job, stub_from_probe, transcript_plan, window_failed, window_prompt_usd,
)
from src.domain.drive_transcript_windows import plan_windows
from src.domain.drive_index_planner import EXTRACTION_VERSION, change_from_record, classify
from src.domain.drive_transcript_render import TRANSCRIPT_BODY_VERSION
from src.domain.drive_transcript_windows import record_content_key

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=3)


def _cid(digest):
    """The content identity (MULTI_DRIVE_RFC §7.2) a provider hash `digest` gives."""
    return ContentIdentity("quickxor", digest, 1)


def _rec(**kw):
    base = dict(user_id="u", item_id="i1", kind="audio", scope=Scope.NORMAL, state=RecordState.PENDING,
                content_version="c1", content_identity=_cid("h1"), name="a.mp3", mime_type="audio/mpeg", size_bytes=1_000_000, modified_at=OLD)
    base.update(kw)
    base.setdefault("identity", f"microsoft_onedrive:d1:{base['item_id']}")   # MULTI_DRIVE_RFC §7.2
    return DriveIndexRecord(**base)


def _ctx(**kw):
    base = dict(now=NOW, enabled=True, month_usd=0.0, reserved_usd=0.0, cap_usd=20.0)
    base.update(kw)
    return TranscriptContext(**base)


def _key(rec):
    return record_content_key(rec)[0]


def _current(rec, **kw):
    return replace(rec, transcript_state="current", transcript_key=_key(rec), transcript_version=TRANSCRIBE_VERSION,
                   transcript_body_version=TRANSCRIPT_BODY_VERSION, **kw)


def _hk(rec):
    return _key(rec)


# -- the cap knob (ruling T-R3) -----------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (None, 20.0), ("", 20.0), ("  ", 20.0), ("10", 10.0), ("10.5", 10.5), ("500", 20.0), ("20", 20.0),
    ("abc", 20.0), ("nan", 20.0), ("inf", 20.0), ("-5", 20.0), ("0", 0.0),
])
def test_effective_cap_can_only_lower(raw, expected):
    assert effective_monthly_cap(raw) == expected


def test_effective_cap_default_is_the_param():
    assert effective_monthly_cap(None) == TRANSCRIBE_USD_PER_MONTH


# -- the table ----------------------------------------------------------------------------------------------------

def test_new_media_record_is_ready():
    out = transcript_plan(_rec(), _ctx())
    assert out.transcript_state == "ready" and out.transcript_wake_at is None and out.transcript_hold is None


def test_ready_to_working_to_waiting_to_current():
    rec = transcript_plan(_rec(), _ctx())
    started = start_file(rec, _ctx(), MediaProbe(duration_s=3600, has_audio=True))
    assert started.outcome is StartOutcome.STARTED
    assert started.rec.transcript_state == "working" and started.rec.transcript_key == _key(rec)
    assert transcript_plan(started.rec, started.ctx).transcript_state == "working"
    waiting = replace(started.rec, transcript_state="waiting")
    assert transcript_plan(waiting, started.ctx).transcript_state == "waiting"
    # the host's index run left the record indexed before the stage marks it current (T-R24: never `pending`)
    done = mark_current(replace(waiting, state=RecordState.INDEXED), NOW, seconds=3600, speakers=2, languages=("uk",))
    assert done.transcript_state == "current" and done.model_read_at == NOW
    assert done.transcript_version == TRANSCRIBE_VERSION and done.transcript_body_version == TRANSCRIPT_BODY_VERSION
    assert (done.transcript_seconds, done.transcript_speakers, done.transcript_languages) == (3600, 2, ("uk",))
    assert transcript_plan(done, _ctx()) == done


def test_kill_switch_off_is_none_work():
    rec = transcript_plan(_rec(), _ctx(enabled=False))
    assert rec.transcript_state == "ready"                       # planned, so turning it on needs no re-plan
    out = start_file(rec, _ctx(enabled=False), MediaProbe(duration_s=600, has_audio=True))
    assert out.outcome is StartOutcome.DISABLED and out.rec == rec and out.reservation is None
    assert out.ctx.reserved_usd == 0.0


def test_switching_off_mid_flight_keeps_in_flight_jobs():
    working = replace(_current(_rec()), transcript_state="working")
    waiting = replace(working, transcript_state="waiting")
    for rec in (working, waiting):
        assert transcript_plan(rec, _ctx(enabled=False)).transcript_state == rec.transcript_state


def test_disabled_writes_no_metadata_stub():
    rec = _rec(size_bytes=TRANSCRIBE_MAX_BYTES + 1)
    assert transcript_plan(rec, _ctx(enabled=False)).transcript_stub == ""
    assert transcript_plan(rec, _ctx(enabled=False)).transcript_state == "ready"


def test_cap_hold_parks_the_whole_file():
    seconds = 0.06 / TRANSCRIBE_HOUR_USD_ESTIMATE * 3600
    ctx = _ctx(cap_usd=20.0, month_usd=19.95)                   # 0.05 left, estimate 0.06
    assert estimate_usd(seconds) == pytest.approx(0.06)
    assert not fits(estimate_usd(seconds), ctx)
    out = start_file(transcript_plan(_rec(), ctx), ctx, MediaProbe(duration_s=seconds, has_audio=True))
    assert out.outcome is StartOutcome.PARKED and out.reservation is None
    assert out.rec.transcript_state == "parked"
    assert out.rec.transcript_wake_at == datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert out.ctx == ctx                                       # nothing reserved, nothing partial


def test_exact_fit_is_allowed():
    assert fits(1.0, _ctx(cap_usd=20.0, month_usd=19.0))
    assert fits(0.0, _ctx(cap_usd=20.0, month_usd=20.0))


def test_pending_reservations_count_against_cap():
    ctx = _ctx(cap_usd=20.0, month_usd=15.0, reserved_usd=4.5)
    assert not fits(1.0, ctx)
    assert fits(0.5, ctx)


def test_month_boundary_wakes_parked():
    parked = replace(transcript_plan(_rec(), _ctx()), transcript_state="parked",
                     transcript_wake_at=datetime(2026, 11, 1, tzinfo=timezone.utc), transcript_hold_version=_hk(_rec()))
    still = transcript_plan(parked, _ctx(now=datetime(2026, 10, 31, 23, 59, tzinfo=timezone.utc)))
    assert still.transcript_state == "parked" and still.transcript_wake_at == parked.transcript_wake_at
    woken = transcript_plan(parked, _ctx(now=datetime(2026, 11, 1, tzinfo=timezone.utc)))
    assert woken.transcript_state == "ready" and woken.transcript_wake_at is None and woken.transcript_hold is None


def test_december_parks_to_january():
    ctx = _ctx(now=datetime(2026, 12, 15, tzinfo=timezone.utc), month_usd=20.0)
    out = start_file(transcript_plan(_rec(), ctx), ctx, MediaProbe(duration_s=3600, has_audio=True))
    assert out.rec.transcript_wake_at == datetime(2027, 1, 1, tzinfo=timezone.utc)


def test_new_content_version_resets_attempts_and_holds():
    rec = _rec()
    failed = replace(rec, transcript_state="failed", transcript_attempts=MAX_ATTEMPTS, transcript_hold_version=_hk(rec))
    assert transcript_plan(failed, _ctx()).transcript_state == "failed"
    edited = replace(failed, content_version="c2", content_identity=_cid("h2"))
    out = transcript_plan(edited, _ctx())
    assert (out.transcript_state, out.transcript_attempts, out.transcript_hold, out.transcript_hold_version) == (
        "ready", 0, None, "")
    r = reset_on_new_version(edited)
    assert r.transcript_attempts == 0 and r.transcript_hold_version == "" and r.transcript_wake_at is None


def test_reset_keeps_attempts_for_the_same_version():
    rec = _rec()
    tagged = replace(rec, transcript_attempts=2, transcript_hold_version=_hk(rec))
    assert reset_on_new_version(tagged) == tagged


def test_edit_cooldown_holds_24h_after_last_transcription():
    done = _current(_rec(), model_read_at=NOW - timedelta(hours=2))
    edited = replace(done, content_version="c2", content_identity=_cid("h2"))
    out = transcript_plan(edited, _ctx())
    assert out.transcript_state == "held" and out.transcript_hold == "cooldown"
    assert out.transcript_wake_at == done.model_read_at + TRANSCRIBE_REREAD_COOLDOWN
    later = transcript_plan(out, _ctx(now=out.transcript_wake_at))
    assert later.transcript_state == "ready" and later.transcript_hold is None and later.transcript_wake_at is None


def test_second_edit_inside_cooldown_stays_held():
    done = _current(_rec(), model_read_at=NOW - timedelta(hours=2))
    once = transcript_plan(replace(done, content_version="c2", content_identity=_cid("h2")), _ctx())
    twice = transcript_plan(replace(once, content_version="c3", content_identity=_cid("h3")), _ctx())
    assert twice.transcript_state == "held" and twice.transcript_wake_at == once.transcript_wake_at


def test_edit_after_cooldown_is_ready():
    done = _current(_rec(), model_read_at=NOW - TRANSCRIBE_REREAD_COOLDOWN - timedelta(minutes=1))
    out = transcript_plan(replace(done, content_version="c2", content_identity=_cid("h2")), _ctx())
    assert out.transcript_state == "ready"


def test_quiet_period_holds_a_recording_still_uploading():
    rec = _rec(dirty_since=NOW - timedelta(minutes=1), modified_at=NOW - timedelta(minutes=1))
    out = transcript_plan(rec, _ctx())
    assert out.transcript_state == "held" and out.transcript_hold == "quiet"
    assert out.transcript_wake_at == rec.modified_at + QUIET
    over = transcript_plan(out, _ctx(now=out.transcript_wake_at))
    assert over.transcript_state == "ready" and over.transcript_hold is None and over.transcript_wake_at is None


def test_failed_at_max_attempts_has_no_stub():
    rec = _rec()
    out = rec
    for i in range(MAX_ATTEMPTS):
        out = transcript_plan(charge_attempt(out), _ctx())
        assert out.transcript_attempts == i + 1
        assert out.transcript_state == ("failed" if i + 1 >= MAX_ATTEMPTS else "ready")
    assert out.transcript_stub == "" and out.transcript_state == "failed"
    assert transcript_plan(out, _ctx()) == out
    assert out.transcript_hold_version == _hk(rec)


def test_too_large_by_metadata_is_a_stub_without_probe():
    rec = _rec(size_bytes=TRANSCRIBE_MAX_BYTES + 1)
    out = transcript_plan(rec, _ctx())
    assert (out.transcript_state, out.transcript_stub) == ("current", "too_large")
    assert (out.state, out.reason) == (RecordState.METADATA_ONLY, Reason.TOO_LARGE)
    assert out.transcript_key == _key(rec) and out.transcript_body_version == TRANSCRIPT_BODY_VERSION
    assert transcript_plan(out, _ctx()) == out
    assert transcript_plan(_rec(size_bytes=TRANSCRIBE_MAX_BYTES), _ctx()).transcript_stub == ""


def test_stubs_from_the_probe():
    assert stub_from_probe(MediaProbe(duration_s=100, has_audio=False)) is TranscriptStub.NO_AUDIO
    assert stub_from_probe(MediaProbe(duration_s=TRANSCRIBE_MAX_SECONDS + 1, has_audio=True)) is TranscriptStub.TOO_LONG
    assert stub_from_probe(MediaProbe(duration_s=TRANSCRIBE_MAX_SECONDS, has_audio=True)) is None
    out = start_file(transcript_plan(_rec(), _ctx()), _ctx(), MediaProbe(duration_s=10, has_audio=False))
    assert out.outcome is StartOutcome.STUBBED and out.reservation is None
    assert (out.rec.transcript_state, out.rec.transcript_stub) == ("current", "no_audio")
    assert out.ctx.reserved_usd == 0.0


def test_outage_writes_no_record_state():
    rec = transcript_plan(_rec(), _ctx())
    until = NOW + timedelta(minutes=30)
    assert queue_blocked(NOW, until) and not queue_blocked(until, until) and not queue_blocked(NOW, None)
    # the backoff is not an input of the plan and no function turns an outage into a record: the record stays ready
    assert "backoff" not in " ".join(inspect.signature(transcript_plan).parameters)
    assert not any("backoff" in f for f in TranscriptContext.__dataclass_fields__)
    assert transcript_plan(rec, _ctx()) == rec and rec.transcript_state == "ready"


def test_plan_reads_no_store():
    assert list(inspect.signature(transcript_plan).parameters) == ["rec", "ctx"]
    src = inspect.getsource(pm)
    assert "src.ports" not in src and "src.adapters" not in src and "src.services" not in src
    assert not inspect.iscoroutinefunction(transcript_plan)


def test_reserved_at_fit_not_at_submit():
    ctx = _ctx(cap_usd=1.0, month_usd=0.0)
    probe = MediaProbe(duration_s=3600 * 3, has_audio=True)       # 3 h = about 0.62 USD with the overlaps
    first = start_file(transcript_plan(_rec(item_id="a", content_identity=_cid("ha")), ctx), ctx, probe)
    assert first.outcome is StartOutcome.STARTED
    assert first.reservation is not None and first.reservation.usd == pytest.approx(file_estimate_usd(3 * 3600))
    assert first.reservation.month == "2026-10"
    assert first.ctx.reserved_usd == pytest.approx(file_estimate_usd(3 * 3600))           # held before any window is extracted or submitted
    second = start_file(transcript_plan(_rec(item_id="b", content_identity=_cid("hb")), first.ctx), first.ctx, probe)
    assert second.outcome is StartOutcome.PARKED                  # fits alone (0.62 <= 1.0), not after the first's hold
    assert fits(file_estimate_usd(3 * 3600), ctx)


def test_release_returns_the_reservation():
    res, held = reserve_if_fits(0.6, _ctx(cap_usd=1.0))
    assert res is not None and held.reserved_usd == pytest.approx(0.6)
    assert release(held, res).reserved_usd == pytest.approx(0.0)
    assert release(_ctx(), res).reserved_usd == 0.0               # never negative


def test_unshared_key_never_hits_another_item():
    a = _rec(item_id="a", content_identity=None)
    b = _rec(item_id="b", content_identity=None)
    assert _key(a) != _key(b)
    done = _current(a, model_read_at=OLD)
    # b has a's key on its record (impossible by construction, but the plan must not call it current)
    other = replace(b, transcript_state="current", transcript_key=_key(done), transcript_version=TRANSCRIBE_VERSION,
                    transcript_body_version=TRANSCRIPT_BODY_VERSION)
    assert transcript_plan(other, _ctx()).transcript_state == "ready"
    shared_a, shared_b = _rec(item_id="a"), _rec(item_id="b")
    assert _key(shared_a) == _key(shared_b)                       # a shared hash does share


@pytest.mark.parametrize("change", [
    dict(inaccessible_since=NOW), dict(kind="pdf"), dict(scope=Scope.OPAQUE_ROOT), dict(scope=Scope.INSIDE_OPAQUE),
    dict(is_folder=True), dict(state=RecordState.DELETING), dict(state=RecordState.UNRESOLVED),
    dict(state=RecordState.METADATA_ONLY, reason=Reason.MALWARE),
    dict(state=RecordState.METADATA_ONLY, reason=Reason.DOT_NAME),
    dict(state=RecordState.METADATA_ONLY, reason=Reason.KIND_NOT_ADMITTED), dict(content_version=""),
])
def test_ineligible_is_none(change):
    out = transcript_plan(_rec(transcript_state="ready", **change), _ctx())
    assert out.transcript_state == "none" and out.transcript_wake_at is None


def test_inaccessible_is_none():
    rec = _current(_rec(), inaccessible_since=NOW)
    assert transcript_plan(rec, _ctx()).transcript_state == "none"


def test_initial_build_never_pays_transcribe():
    fields = set(TranscriptContext.__dataclass_fields__)
    assert not {f for f in fields if "initial" in f}
    assert pm.available_usd(_ctx(cap_usd=20.0, month_usd=20.0)) == 0.0   # no allowance beyond the cap
    assert not fits(0.01, _ctx(cap_usd=20.0, month_usd=20.0))


def test_current_needs_the_body_version():
    stale = replace(_current(_rec()), transcript_body_version=TRANSCRIPT_BODY_VERSION - 1)
    assert transcript_plan(stale, _ctx()).transcript_state == "ready"       # the stage finds the store hit and re-renders


def test_current_stub_survives_the_plan():
    stub = _current(_rec(state=RecordState.METADATA_ONLY), transcript_stub="no_speech")   # a stub is metadata_only
    assert transcript_plan(stub, _ctx()) == stub


def test_stale_key_working_job_resets_to_ready():
    working = replace(_current(_rec()), transcript_state="working")
    edited = replace(working, content_version="c2", content_identity=_cid("h2"))
    assert transcript_plan(edited, _ctx()).transcript_state == "ready"


# -- the job ------------------------------------------------------------------------------------------------------

def _win(state, i=0, **kw):
    return JobWindow(spec=WindowSpec(index=i, start_s=i * 1740.0, duration_s=1800.0), state=state, **kw)


def test_state_for_job():
    assert state_for_job(TranscriptJob("u", "i", "k")) == "working"
    assert state_for_job(TranscriptJob("u", "i", "k", windows=(_win("submitted"), _win("done", 1)))) == "waiting"
    assert state_for_job(TranscriptJob("u", "i", "k", windows=(_win("submitted"), _win("uploaded", 1)))) == "working"
    assert state_for_job(TranscriptJob("u", "i", "k", windows=(_win("submitted"), _win("failed", 1)))) == "working"


def test_invalid_and_coverage_get_max_attempts_then_a_gap():
    for check in (WindowCheck.INVALID, WindowCheck.COVERAGE):
        w, ctx = _win("submitted"), _ctx()
        for n in range(1, MAX_ATTEMPTS):
            r = window_failed(w, check, ctx)
            w, ctx = r.window, r.ctx
            assert r.action is WindowAction.RESUBMIT and w.attempts == n and w.state == "uploaded"
        r = window_failed(w, check, ctx)
        assert r.action is WindowAction.GAP and r.window.gap and r.window.state == "done" and r.reservation is None


def test_unparseable_stamp_is_an_invalid_attempt_not_a_crash():
    r = window_failed(_win("submitted"), WindowCheck.INVALID, _ctx())
    assert r.action is WindowAction.RESUBMIT and r.window.attempts == 1


def test_time_validator_gets_one_resubmit_then_a_gap():
    r = window_failed(_win("submitted"), WindowCheck.TIME, _ctx())
    assert r.action is WindowAction.RESUBMIT and r.window.time_resubmits == 1 and r.window.state == "uploaded"
    r = window_failed(r.window, WindowCheck.TIME, r.ctx)
    assert r.action is WindowAction.GAP and r.window.gap and r.window.state == "done"


def test_interpolation_share_is_resubmitted_once_then_accepted():
    r = window_failed(_win("submitted"), WindowCheck.INTERPOLATION, _ctx())
    assert r.action is WindowAction.RESUBMIT and r.window.interp_resubmits == 1
    r = window_failed(r.window, WindowCheck.INTERPOLATION, r.ctx)
    assert r.action is WindowAction.ACCEPT and not r.window.gap and r.window.state == "done"


def test_a_refused_window_is_a_gap_at_once():
    r = window_failed(_win("submitted"), WindowCheck.REFUSED, _ctx())
    assert r.action is WindowAction.GAP and r.window.gap and r.window.attempts == 0


def test_a_resubmit_is_reserved():
    r = window_failed(_win("submitted"), WindowCheck.TIME, _ctx())
    # T-R19: a window's reservation is its audio estimate plus the fixed per-window prompt allowance
    assert window_prompt_usd() > 0
    assert r.reservation is not None and r.reservation.usd == pytest.approx(estimate_usd(1800.0) + window_prompt_usd())
    assert r.ctx.reserved_usd == pytest.approx(r.reservation.usd)


def test_a_resubmit_that_does_not_fit_parks():
    ctx = _ctx(cap_usd=20.0, month_usd=20.0 - 0.01)               # a 30-minute window costs 0.10
    r = window_failed(_win("submitted"), WindowCheck.COVERAGE, ctx)
    assert r.action is WindowAction.PARK and r.reservation is None and r.ctx == ctx
    assert r.window.attempts == 1 and r.window.state == "uploaded"   # the attempt stays counted
    parked = park(_rec(), ctx)
    assert parked.transcript_state == "parked" and parked.transcript_wake_at == datetime(2026, 11, 1, tzinfo=timezone.utc)


def test_a_gap_or_accept_needs_no_money():
    full = _ctx(cap_usd=1.0, month_usd=1.0)
    assert window_failed(_win("submitted"), WindowCheck.REFUSED, full).action is WindowAction.GAP
    w = replace(_win("submitted"), interp_resubmits=1)
    assert window_failed(w, WindowCheck.INTERPOLATION, full).action is WindowAction.ACCEPT


def test_file_estimate_counts_the_overlaps():
    d = 5000.0
    windows = plan_windows(d)
    assert sum(w.duration_s for w in windows) > d
    # T-R19: every planned window (overlaps included) pays its audio and one prompt allowance
    assert file_estimate_usd(d) == pytest.approx(estimate_usd(sum(w.duration_s for w in windows))
                                                 + len(windows) * window_prompt_usd())
    assert file_estimate_usd(d) > estimate_usd(d)
    # a file whose bare duration fits the cap but whose overlaps do not parks
    bare = estimate_usd(d)
    ctx = _ctx(cap_usd=20.0, month_usd=20.0 - bare - 1e-4)
    assert fits(bare, ctx)
    assert start_file(transcript_plan(_rec(), ctx), ctx, MediaProbe(duration_s=d, has_audio=True)).outcome \
        is StartOutcome.PARKED


# -- review fixes (fix round 1) -----------------------------------------------------------------------------------

def test_a_transcribe_version_bump_changes_nothing():
    cur = _current(_rec(state=RecordState.INDEXED))   # a finished transcript's record is indexed (T-R24)
    bumped = replace(cur, transcript_version=TRANSCRIBE_VERSION - 1)
    assert transcript_plan(bumped, _ctx()) == bumped
    rec = _rec()
    failed = replace(rec, transcript_state="failed", transcript_attempts=MAX_ATTEMPTS, transcript_hold_version=_hk(rec),
                     transcript_version=TRANSCRIBE_VERSION - 1)
    assert transcript_plan(failed, _ctx()).transcript_state == "failed"
    assert transcript_plan(transcript_plan(failed, _ctx()), _ctx()) == transcript_plan(failed, _ctx())


def test_release_in_a_later_month_leaves_that_months_reservations():
    res, held = reserve_if_fits(0.6, _ctx(cap_usd=1.0))            # made in October
    assert res is not None and res.month == "2026-10"
    november = replace(held, now=datetime(2026, 11, 2, tzinfo=timezone.utc), reserved_usd=0.3)   # November's own hold
    assert release(november, res) == november
    assert release(held, res).reserved_usd == pytest.approx(0.0)    # same month: released


def test_a_same_key_edit_keeps_an_in_flight_job():
    fresh = NOW - timedelta(minutes=1)
    for state in ("working", "waiting"):
        job = replace(_current(_rec()), transcript_state=state, dirty_since=fresh, modified_at=fresh,
                      content_version="c2")                          # new cTag, same bytes: same key
        assert record_content_key(job)[0] == _key(_rec())
        assert transcript_plan(job, _ctx()).transcript_state == state


def test_stub_survives_a_reobservation():
    rec = transcript_plan(_rec(size_bytes=TRANSCRIBE_MAX_BYTES + 1), _ctx())
    assert (rec.state, rec.reason) == (RecordState.METADATA_ONLY, Reason.TOO_LARGE)
    assert rec.extraction_version == EXTRACTION_VERSION != _rec().extraction_version and rec.dirty_since is None
    again = classify(change_from_record(rec), Scope.NORMAL, None, rec)
    assert (again.state, again.reason) == (RecordState.METADATA_ONLY, Reason.TOO_LARGE)
    for stub in ("no_speech",):
        done = mark_current(_rec(dirty_since=NOW, modified_at=NOW), NOW, seconds=60, speakers=0, languages=(), stub=stub)
        assert done.extraction_version != 0 and done.dirty_since is None
        again = classify(change_from_record(done), Scope.NORMAL, None, done)
        assert (again.state, again.reason) == (RecordState.METADATA_ONLY, Reason.NO_TEXT)
    probed = start_file(_rec(), _ctx(), MediaProbe(duration_s=TRANSCRIBE_MAX_SECONDS + 1, has_audio=True)).rec
    again = classify(change_from_record(probed), Scope.NORMAL, None, probed)
    assert again.state is RecordState.METADATA_ONLY


@pytest.mark.parametrize("duration", [0.0, -5.0, float("nan"), float("inf"), float("-inf")])
def test_unusable_probe_duration_is_a_charged_attempt(duration):
    ctx = _ctx()
    out = start_file(transcript_plan(_rec(), ctx), ctx, MediaProbe(duration_s=duration, has_audio=True))
    assert out.outcome is StartOutcome.UNREADABLE and out.reservation is None and out.ctx == ctx
    assert out.rec.transcript_attempts == 1 and out.rec.transcript_state == "ready" and out.rec.transcript_stub == ""
    rec = out.rec
    for _ in range(MAX_ATTEMPTS - 1):
        rec = start_file(transcript_plan(rec, ctx), ctx, MediaProbe(duration_s=duration, has_audio=True)).rec
    assert rec.transcript_state == "failed" and rec.transcript_stub == ""


def test_no_audio_wins_over_a_garbage_duration():
    out = start_file(_rec(), _ctx(), MediaProbe(duration_s=float("nan"), has_audio=False))
    assert out.outcome is StartOutcome.STUBBED and out.rec.transcript_stub == "no_audio"


def test_cooldown_runs_from_the_purchase_not_only_the_completion():
    started = start_file(transcript_plan(_rec(), _ctx()), _ctx(), MediaProbe(duration_s=3600, has_audio=True))
    assert started.rec.model_read_at == NOW
    later = NOW + timedelta(hours=2)                                 # quiet period long over
    edited = replace(started.rec, content_version="c2", content_identity=_cid("h2"), dirty_since=None)
    out = transcript_plan(edited, _ctx(now=later))
    assert out.transcript_state == "held" and out.transcript_hold == "cooldown"
    assert out.transcript_wake_at == NOW + TRANSCRIBE_REREAD_COOLDOWN


# -- properties ---------------------------------------------------------------------------------------------------

@pytest.mark.slow
def test_plan_idempotent_property():
    base = _rec()
    hk = _hk(base)
    variants = []
    states = ["none", "ready", "working", "waiting", "current", "held", "parked", "failed"]
    for state, attempts, tag, keyed, wake, dirty, size, done, version, hold in itertools.product(
            states, (0, 1, MAX_ATTEMPTS), ("", hk, "9:other"), (True, False),
            (None, NOW - timedelta(days=1), NOW + timedelta(days=1)),
            (None, NOW - timedelta(minutes=1)), (1000, TRANSCRIBE_MAX_BYTES + 1),
            (None, NOW - timedelta(hours=1)), (TRANSCRIBE_VERSION, TRANSCRIBE_VERSION - 1), (None, "resubmit")):
        variants.append(replace(
            base, transcript_state=state, transcript_attempts=attempts, transcript_hold_version=tag,
            transcript_key=_key(base) if keyed else "x", transcript_version=version, transcript_hold=hold,
            transcript_body_version=TRANSCRIPT_BODY_VERSION, transcript_wake_at=wake, dirty_since=dirty,
            modified_at=dirty or OLD, size_bytes=size, model_read_at=done))
    assert len(variants) > 1000
    for enabled in (True, False):
        ctx = _ctx(enabled=enabled)
        for rec in variants:
            once = transcript_plan(rec, ctx)
            assert transcript_plan(once, ctx) == once, (rec.transcript_state, rec.transcript_attempts)


def test_mid_job_park_wakes_as_working_and_reserves_only_the_window():
    ctx = _ctx(cap_usd=1.0)
    probe = MediaProbe(duration_s=7200, has_audio=True)
    started = start_file(transcript_plan(_rec(), ctx), ctx, probe)
    assert started.outcome is StartOutcome.STARTED
    held = started.reservation.usd
    full = replace(started.ctx, month_usd=1.0 - held - 0.01)         # one window (0.10) no longer fits
    r = window_failed(_win("submitted"), WindowCheck.COVERAGE, full)
    assert r.action is WindowAction.PARK and r.ctx.reserved_usd == pytest.approx(held)   # original still held
    parked = park(started.rec, full, resubmit=True)
    assert (parked.transcript_state, parked.transcript_hold) == ("parked", "resubmit")
    assert parked.transcript_key == _key(_rec())
    assert transcript_plan(parked, full) == parked                   # stays parked until the wake-up
    nov = _ctx(now=datetime(2026, 11, 1, tzinfo=timezone.utc), cap_usd=1.0, reserved_usd=held)
    woken = transcript_plan(parked, nov)
    assert woken.transcript_state == "working" and woken.transcript_hold is None
    assert transcript_plan(woken, nov) == woken
    resumed = resume_window(r.window, nov)
    assert resumed.action is WindowAction.RESUBMIT
    window = estimate_usd(1800.0) + window_prompt_usd()               # T-R19: audio + the per-window prompt allowance
    assert resumed.reservation.usd == pytest.approx(window)
    assert resumed.ctx.reserved_usd == pytest.approx(held + window)   # no whole-file re-reservation


def test_resume_that_still_does_not_fit_parks_again():
    r = resume_window(_win("uploaded", attempts=1), _ctx(cap_usd=1.0, month_usd=1.0))
    assert r.action is WindowAction.PARK


def test_key_change_while_parked_mid_job_resets():
    ctx = _ctx(cap_usd=1.0)
    started = start_file(transcript_plan(_rec(), ctx), ctx, MediaProbe(duration_s=7200, has_audio=True))
    parked = park(started.rec, ctx, resubmit=True)
    edited = replace(parked, content_version="c2", content_identity=_cid("h2"))
    out = transcript_plan(edited, replace(ctx, now=NOW + timedelta(days=30)))
    assert out.transcript_state == "ready" and out.transcript_hold is None and out.transcript_attempts == 0

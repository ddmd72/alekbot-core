"""Plan rules the transcript stage relies on (plan Task 12): a container without a duration header is not unreadable
(G35, the owner's browser-recorded webm), a record read under another EXTRACTION_VERSION is re-finalized (G31), and a
copy waits for its other file (G12)."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.drive_content_identity import ContentIdentity
from src.domain.drive_index import EXTRACTION_VERSION, TRANSCRIBE_VERSION, DriveIndexRecord, RecordState, Scope
from src.domain.drive_index_params import TRANSCRIBE_MAX_SECONDS
from src.domain.drive_transcript import MediaProbe
from src.domain.drive_transcript_plan import (
    StartOutcome, TranscriptContext, file_estimate_usd, start_file, transcript_plan, wait_for_copy,
)
from src.domain.drive_transcript_render import TRANSCRIPT_BODY_VERSION

NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)


def _cid(digest):
    """The content identity (MULTI_DRIVE_RFC §7.2) a provider hash `digest` gives."""
    return ContentIdentity("quickxor", digest, 1)


def _rec(**kw):
    base = dict(user_id="u", item_id="i1", kind="audio", scope=Scope.NORMAL, state=RecordState.PENDING,
                content_version="c1", content_identity=_cid("h1"), name="a.webm", mime_type="audio/webm", size_bytes=1_000,
                modified_at=NOW - timedelta(days=3))
    base.update(kw)
    base.setdefault("identity", f"microsoft_onedrive:d1:{base['item_id']}")   # MULTI_DRIVE_RFC §7.2
    return DriveIndexRecord(**base)


def _ctx(**kw):
    base = dict(now=NOW, enabled=True, month_usd=0.0, reserved_usd=0.0, cap_usd=20.0)
    base.update(kw)
    return TranscriptContext(**base)


_WEBM = MediaProbe(duration_s=0.0, has_audio=True, container="matroska,webm", duration_known=False)


def test_a_webm_without_a_duration_starts_on_the_worst_case():
    out = start_file(transcript_plan(_rec(), _ctx()), _ctx(), _WEBM)
    assert out.outcome is StartOutcome.STARTED and out.rec.transcript_attempts == 0
    assert out.reservation.usd == pytest.approx(file_estimate_usd(float(TRANSCRIBE_MAX_SECONDS)))
    assert out.ctx.reserved_usd == pytest.approx(out.reservation.usd)
    assert out.rec.transcript_state == "working" and out.rec.transcript_seconds == 0 and out.rec.model_read_at == NOW


def test_a_webm_that_cannot_reserve_its_worst_case_parks():
    out = start_file(_rec(), _ctx(cap_usd=file_estimate_usd(float(TRANSCRIBE_MAX_SECONDS)) / 2), _WEBM)
    assert out.outcome is StartOutcome.PARKED and out.reservation is None


def test_a_webm_without_audio_is_still_a_no_audio_stub():
    out = start_file(_rec(), _ctx(), replace(_WEBM, has_audio=False))
    assert out.outcome is StartOutcome.STUBBED and out.rec.transcript_stub == "no_audio"


def _current(**kw):
    return _rec(state=RecordState.INDEXED, indexed_generation=1, indexed_version="c1", transcript_state="current",
                transcript_key=_cid("h1").render(), transcript_version=TRANSCRIBE_VERSION,
                transcript_body_version=TRANSCRIPT_BODY_VERSION, **kw)


def test_a_record_read_under_another_extraction_version_is_queued_again():
    assert transcript_plan(_current(extraction_version=EXTRACTION_VERSION), _ctx()).transcript_state == "current"
    assert transcript_plan(_current(extraction_version=EXTRACTION_VERSION - 1), _ctx()).transcript_state == "ready"
    assert transcript_plan(_current(extraction_version=0), _ctx()).transcript_state == "current"   # never stamped


def test_a_copy_waits_at_its_key_and_the_plan_keeps_it():
    waiting = wait_for_copy(_rec())
    assert waiting.transcript_state == "waiting" and waiting.transcript_key == _cid("h1").render()
    assert transcript_plan(waiting, _ctx()).transcript_state == "waiting"
    edited = replace(waiting, content_identity=_cid("h2"), content_version="c2")
    assert transcript_plan(edited, _ctx()).transcript_state == "ready"


# -- T-R21: a charged attempt backs off ---------------------------------------------------------------------------------

def test_a_charge_with_now_is_held_for_retry_until_its_wake_up():
    from src.domain.drive_index_params import MAX_ATTEMPTS, RETRY_BASE
    from src.domain.drive_transcript_plan import charge_attempt
    held = charge_attempt(_rec(), now=NOW)
    assert (held.transcript_state, held.transcript_hold, held.transcript_attempts) == ("held", "retry", 1)
    assert held.transcript_wake_at == NOW + RETRY_BASE and held.transcript_hold_version == _cid("h1").render()
    assert transcript_plan(held, _ctx(now=NOW + RETRY_BASE / 2)).transcript_state == "held"
    woken = transcript_plan(held, _ctx(now=NOW + RETRY_BASE))
    assert woken.transcript_state == "ready" and woken.transcript_attempts == 1 and woken.transcript_wake_at is None
    second = charge_attempt(woken, now=NOW)
    assert second.transcript_wake_at == NOW + 2 * RETRY_BASE
    edited = replace(held, content_identity=_cid("h2"), content_version="c2")
    assert transcript_plan(edited, _ctx()).transcript_state == "ready"   # a new version clears the hold (D42)
    assert charge_attempt(replace(_rec(), transcript_attempts=MAX_ATTEMPTS - 1), now=NOW).transcript_state == "failed"
    assert charge_attempt(_rec()).transcript_state == "ready"   # without `now`: unchanged for older callers

"""Review fix T-R24 of Task 14A (USER_DRIVE_INDEXING_RFC §27.3, §27.11, §5.4): a card-capped recording is out of the
transcript pipeline, a `current` record that is `pending` again is re-indexed from the store, and a failed index run
shows no transcript line on the card. Synthetic records only."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from src.domain.drive_content_identity import ContentIdentity
from src.domain.drive_index import DriveIndexRecord, RecordState, Scope
from src.domain.drive_transcript_plan import (
    TranscriptContext, leaves_job, mark_current, transcript_plan,
)
from src.domain.drive_transcript_render import card_segment

NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
CTX = TranscriptContext(now=NOW, enabled=True, month_usd=0.0, reserved_usd=0.0, cap_usd=20.0)


def _cid(digest):
    """The content identity (MULTI_DRIVE_RFC §7.2) a provider hash `digest` gives."""
    return ContentIdentity("quickxor", digest, 1)


def _rec(**kw):
    base = dict(user_id="u", item_id="i1", kind="audio", scope=Scope.NORMAL, state=RecordState.PENDING,
                content_version="c1", content_identity=_cid("h1"), name="a.mp3", size_bytes=1_000_000,
                modified_at=NOW - timedelta(days=3))
    base.update(kw)
    base.setdefault("identity", f"microsoft_onedrive:d1:{base['item_id']}")   # MULTI_DRIVE_RFC §7.2
    return DriveIndexRecord(**base)


def _current(state):
    done = mark_current(_rec(), NOW - timedelta(days=2), seconds=600, speakers=2, languages=("en",))
    return replace(done, state=state)


def test_current_and_pending_is_ready_again():
    assert transcript_plan(_current(RecordState.PENDING), CTX).transcript_state == "ready"


def test_current_and_indexed_stays_current():
    rec = _current(RecordState.INDEXED)
    assert transcript_plan(rec, CTX) == rec


def test_current_and_metadata_only_stays_current():
    rec = _current(RecordState.METADATA_ONLY)
    assert transcript_plan(rec, CTX).transcript_state == "current"


def test_card_capped_is_none_and_leaves_its_job():
    working = _rec(transcript_state="working", transcript_key="h1")
    capped = replace(working, card_capped=True)
    planned = transcript_plan(capped, CTX)
    assert planned.transcript_state == "none" and leaves_job(capped, planned)
    assert transcript_plan(_rec(card_capped=True), CTX).transcript_state == "none"


def test_a_released_card_cap_is_ready_again():
    released = replace(_rec(card_capped=True, transcript_state="none"), card_capped=False)
    assert transcript_plan(released, CTX).transcript_state == "ready"


def test_card_segment_skips_a_failed_record():
    assert card_segment(_current(RecordState.INDEXED)) == "transcript: 10 min, 2 speakers, en"
    assert card_segment(_current(RecordState.FAILED)) is None

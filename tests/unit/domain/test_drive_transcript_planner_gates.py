"""Planner gates for transcribed kinds (USER_DRIVE_INDEXING_RFC §27.3, Δ95; plan G4): media classify as pending, never
reach the index step, and a transcribe link is never extractor-stale."""
from datetime import datetime, timedelta, timezone

from src.domain.drive_card import card_hash
from src.domain.drive_index import (
    DriveChange, DriveIndexRecord, EpochPhase, Priority, Reason, RecordState, Scope,
)
from src.domain.drive_index_planner import (
    PlanContext, classify, content_due, demoted, extractor_stale, plan_observation, schedule, triage_due, with_card,
)

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
FS = datetime(2026, 9, 1, 8, tzinfo=timezone.utc)
ROOT = "root"
MIB = 1024 * 1024


def ctx():
    return PlanContext(user_id="u", root_item_id=ROOT, epoch=1, phase=EpochPhase.IDLE, now=NOW,
                       place_of=lambda lat, lon: None)


def media(name="memo.m4a", item_id="m", cv="c1", size=1000, **kw):
    return DriveChange(item_id=item_id, name=name, parent_id=ROOT, size_bytes=size, content_version=cv,
                       modified_at=NOW - timedelta(days=1), fs_modified_at=FS, **kw)


def observe(change, existing=None):
    return plan_observation(change, existing, None, None, ctx()).upsert


def test_media_classifies_pending_under_policy_3():
    for name in ("memo.m4a", "clip.mp4"):
        cls = classify(media(name), Scope.NORMAL, None, None)
        assert (cls.state, cls.reason) == (RecordState.PENDING, None), name


def test_media_over_45_mib_is_not_too_large_by_download_cap():
    cls = classify(media(size=600 * MIB), Scope.NORMAL, None, None)
    assert (cls.state, cls.reason) == (RecordState.PENDING, None)


def test_media_outcome_that_stands_is_kept_on_rename():
    first = observe(media())
    stub = demoted(first, Reason.NO_TEXT, NOW)    # a stub outcome stamps extraction_version
    renamed = observe(media(name="memo renamed.m4a"), stub)
    assert (renamed.state, renamed.reason) == (RecordState.METADATA_ONLY, Reason.NO_TEXT)


def test_content_due_false_for_media():
    rec = observe(media())
    assert rec.state is RecordState.PENDING
    assert not content_due(rec) and not content_due(rec, NOW) and not triage_due(rec)


def test_schedule_gives_media_no_content_work():
    rec = observe(media())
    # the only work is the card, in the first-seen class; no content/triage work sits behind it
    assert rec.priority == Priority.FIRST_SEEN
    carded = with_card(rec, card_hash(rec), "s", NOW)
    assert schedule(carded, NOW, first_seen=False).due_at is None


def test_transcribe_link_never_extractor_stale():
    for kind in ("audio", "video"):
        for version in ("1", "2", "99"):
            rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="m", kind=kind, extracted_by=f"transcribe-gemini@{version}")
            assert not extractor_stale(rec, {"parser": 1}), (kind, version)
            assert not extractor_stale(rec, {"transcribe-gemini": 7})


def test_parser_link_on_audio_still_stale():
    rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="m", kind="audio", extracted_by="parser@1")
    assert extractor_stale(rec, {"parser": 2})
    assert not extractor_stale(DriveIndexRecord(user_id="u", connection_id="u", item_id="p", kind="pdf", extracted_by="parser@1"),
                               {"parser": 1})

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.drive_index import DriveIndexRecord, RecordState
from src.domain.drive_index_params import MIRROR_ARCHIVE_GRACE, MIRROR_FAILED_RETRY, MIRROR_ROOT_GRACE
from src.domain.drive_index_planner import backoff
from src.domain.drive_mirror import (
    GONE, KIND_ARCHIVE, KIND_FILE, KIND_FOLDER, MIRROR_ACTIVE, MIRROR_ARCHIVED, MIRROR_DETACHED, UNKNOWN, UNSTORED,
    DeletionNotice, MirrorDoc, MirrorRoot,
)
from src.domain.drive_mirror_names import Placement, temp_name
from src.domain.drive_mirror_plan import (
    MIRROR_PLAN_VERSION, Action, Decision, RootDecision, charged, file_action, folder_action, local_day, mark_decision,
    charged_doc, digest_changed, mark_held, marked, mirror_digest, mirror_eligible, mirror_plan, prune_due, reset_charges, retry_due, root_decision,
    stamped, unmarked, woken,
)
from src.domain import drive_mirror_plan, drive_mirror_render

T = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def _rec(**kw):
    base = dict(user_id="u", item_id="i1", parent_id="p1", name="Interview.mp4", web_url="https://x/1",
                transcript_state="current", transcript_key="k1", transcript_version=1, transcript_stub="",
                transcript_truncated=False, transcript_seconds=2820, transcript_speakers=2,
                transcript_languages=("uk", "en"), transcript_body_version=1)
    base.update(kw)
    return DriveIndexRecord(**base)


def _folder(**kw):
    return DriveIndexRecord(user_id="u", connection_id="u", item_id="f1", parent_id="p0", name="Videos", is_folder=True,
                           **{"state": RecordState.METADATA_ONLY, **kw})


def _doc(**kw):
    base = dict(user_id="u", drive_identity="d", kind=KIND_FILE, source_item_id="i1", source_name="Interview.mp4",
                source_parent_id="p1", mirror_name="Interview.mp4.md", rendered_hash="h1", mirror_item_id="m1")
    base.update(kw)
    return MirrorDoc(**base)


def _place(name="Interview.mp4.md"):
    return Placement(("Videos", name))


# -- digest --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("change", [
    {"name": "Other.mp4"}, {"parent_id": "p2"}, {"web_url": "https://x/2"}, {"transcript_key": "k2"},
    {"transcript_version": 2}, {"transcript_stub": "no_speech"}, {"transcript_truncated": True},
    {"transcript_seconds": 10}, {"transcript_speakers": 3}, {"transcript_languages": ("en",)},
    {"transcript_body_version": 2},
])
def test_digest_changes_on_each_listed_field(change):
    assert mirror_digest(_rec()) != mirror_digest(_rec(**change))


def test_digest_ignores_unlisted_fields():
    assert mirror_digest(_rec()) == mirror_digest(_rec(size_bytes=5, transcript_attempts=3, mirror_due=True))


def test_folder_digest_is_name_and_parent():
    base = mirror_digest(_folder())
    assert base != mirror_digest(replace(_folder(), name="Other"))
    assert base != mirror_digest(replace(_folder(), parent_id="p9"))
    assert base == mirror_digest(replace(_folder(), child_count=9))


def test_digest_changes_on_render_and_plan_version(monkeypatch):
    before = mirror_digest(_rec())
    monkeypatch.setattr(drive_mirror_plan, "RENDER_VERSION", drive_mirror_render.RENDER_VERSION + 1)
    after_render = mirror_digest(_rec())
    monkeypatch.setattr(drive_mirror_plan, "MIRROR_PLAN_VERSION", MIRROR_PLAN_VERSION + 1)
    assert len({before, after_render, mirror_digest(_rec())}) == 3


def test_render_language_not_in_digest():
    from src.domain.drive_mirror_render import render_file
    from src.domain.drive_transcript import Segment, StitchedTranscript, StoredTranscript
    from tests.unit.domain.test_drive_mirror_render import EN, UK
    rec = _rec()
    stored = StoredTranscript("k", StitchedTranscript((Segment(0.0, 1, "hi"),), ("en",), 1, 5.0))
    en = render_file(stored, rec, EN, source_path="p", recorded_on="d")
    uk = render_file(stored, rec, UK, source_path="p", recorded_on="d")
    assert en != uk                                    # the language changes the file ...
    assert mirror_digest(rec) == mirror_digest(replace(rec))   # ... and the digest is a function of the record alone
    assert mirror_plan(stamped(rec)).mirror_due is False


# -- due -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["none", "ready", "working", "waiting", "held", "parked", "failed"])
def test_not_current_is_never_due(state):
    assert mirror_plan(_rec(transcript_state=state)).mirror_due is False


def test_current_is_due_and_stub_too():
    assert mirror_plan(_rec()).mirror_due is True
    assert mirror_plan(_rec(transcript_stub="no_speech", transcript_languages=())).mirror_due is True


def test_folder_due_only_when_mirrored():
    assert mirror_plan(_folder()).mirror_due is False
    mirrored = replace(_folder(), mirrored_digest="old")
    assert mirror_plan(mirrored).mirror_due is True
    assert mirror_plan(replace(mirrored, mirrored_digest=mirror_digest(mirrored))).mirror_due is False


def test_stamped_clears_due():
    rec = mirror_plan(_rec())
    assert rec.mirror_due
    done = stamped(rec)
    assert (done.mirror_due, done.mirrored_digest) == (False, mirror_digest(rec))
    assert mirror_plan(done).mirror_due is False


def test_stamped_folder_makes_a_rename_due():
    folder = stamped(_folder())
    assert mirror_eligible(folder) and not mirror_plan(folder).mirror_due
    assert mirror_plan(replace(folder, name="Renamed")).mirror_due is True


def test_plan_touches_only_mirror_fields():
    rec = _rec(attempts=3, due_at=T, size_bytes=7)
    out = mirror_plan(rec)
    changed = {f for f in rec.__dataclass_fields__ if getattr(rec, f) != getattr(out, f)}
    assert changed <= {"mirror_due", "mirror_retry_at", "mirror_held_digest", "mirrored_digest"}
    assert changed == {"mirror_due"}


def test_held_record_stays_off_the_queue_until_woken():
    rec = mirror_plan(_rec())
    doc, held = charged(_doc(), rec, T)
    assert held.mirror_due is False and held.mirror_held_digest == mirror_digest(rec)
    assert mirror_plan(held).mirror_due is False            # a re-plan of the same digest keeps it off the queue
    back = woken(held)
    assert back.mirror_due is True and back.mirror_held_digest == "" and back.mirror_retry_at is None


def test_new_digest_clears_the_hold_at_once():
    _, held = charged(_doc(), mirror_plan(_rec()), T)
    renamed = mirror_plan(replace(held, name="Renamed.mp4"))
    assert renamed.mirror_due is True and renamed.mirror_held_digest == "" and renamed.mirror_retry_at is None


def test_woken_a_stamped_record_is_not_due():
    rec = stamped(_rec())
    assert woken(rec).mirror_due is False


# -- actions -------------------------------------------------------------------------------------------------------

def test_create_when_no_doc():
    assert file_action(_rec(), None, "h", _place()) is Action.CREATE
    assert folder_action(_folder(), None, _place("Videos")) is Action.CREATE


def test_replace_only_when_hash_differs():
    doc = _doc()
    assert file_action(_rec(), doc, "h2", _place()) is Action.REPLACE
    assert file_action(_rec(), doc, "h1", _place()) is Action.STAMP_ONLY


def test_restore_when_archived_and_current():
    assert file_action(_rec(), _doc(state=MIRROR_ARCHIVED), "h1", _place()) is Action.RESTORE


def test_reattach_when_detached_and_back():
    assert file_action(_rec(), _doc(state=MIRROR_DETACHED), "h1", _place()) is Action.REATTACH


def test_move_on_new_parent_or_name():
    assert file_action(_rec(parent_id="p2"), _doc(), "h1", _place()) is Action.MOVE
    assert file_action(_rec(name="New.mp4"), _doc(), "h1", _place("New.mp4.md")) is Action.MOVE
    assert file_action(_rec(), _doc(), "h1", _place("Interview.mp4~1a2b3c4d.md")) is Action.MOVE   # collision suffix
    assert file_action(_rec(), _doc(), "h2", _place("Interview.mp4~1a2b3c4d.md")) is Action.MOVE   # move before replace


def test_a_stopped_tick_leaves_the_temp_name_and_the_move_is_finished():
    final = "Interview.mp4.md"
    doc = _doc(mirror_name=temp_name(final, "u:file:i1"))   # source fields already current, drive name still temporary
    assert file_action(_rec(), doc, "h1", _place(final)) is Action.MOVE
    assert file_action(_rec(), replace(doc, mirror_name=final), "h1", _place(final)) is Action.STAMP_ONLY


def test_folder_actions():
    doc = _doc(kind=KIND_FOLDER, source_item_id="f1", source_name="Videos", source_parent_id="p0", mirror_name="Videos")
    assert folder_action(_folder(), doc, _place("Videos")) is Action.STAMP_ONLY
    assert folder_action(replace(_folder(), name="V2"), doc, _place("V2")) is Action.MOVE
    assert folder_action(_folder(), replace(doc, mirror_name="Videos~tmp12345678"), _place("Videos")) is Action.MOVE


# -- marks ---------------------------------------------------------------------------------------------------------

def _marked(why=GONE, at=T):
    return marked(_doc(), DeletionNotice("i1", why, at))


def _decide(doc, **kw):
    args = dict(record_present=False, ancestor_reobserving=False, now=T + MIRROR_ARCHIVE_GRACE + timedelta(minutes=1))
    args.update(kw)
    return mark_decision(doc, **args)


def test_mark_created_ready_after_the_grace():
    doc = _marked()
    assert doc.gone_pending and doc.gone_why == GONE and doc.gone_at == T
    assert doc.mark_ready_at == T + MIRROR_ARCHIVE_GRACE
    assert unmarked(doc).gone_pending is False and unmarked(doc).mark_ready_at is None


def test_mark_inside_grace_waits():
    assert _decide(_marked(), now=T + timedelta(minutes=59)) is Decision.WAIT


def test_mark_held_moves_ready_later_never_earlier():
    doc = mark_held(_marked(), T + timedelta(hours=5))
    assert doc.mark_ready_at == T + timedelta(hours=5)
    assert mark_held(doc, T + timedelta(hours=2)).mark_ready_at == T + timedelta(hours=5)
    assert _decide(doc, now=T + timedelta(hours=4)) is Decision.WAIT


def test_mark_waits_while_an_ancestor_reobserves():
    assert _decide(_marked(), ancestor_reobserving=True) is Decision.WAIT


def test_mark_record_back_clears():
    assert _decide(_marked(), record_present=True) is Decision.CLEAR
    assert _decide(_marked(), record_present=True, now=T) is Decision.CLEAR       # even inside the grace


def test_gone_archives():
    assert _decide(_marked(GONE)) is Decision.ARCHIVE


def test_unstored_detaches():
    assert _decide(_marked(UNSTORED)) is Decision.DETACH


def test_unknown_needs_check():
    assert _decide(_marked(UNKNOWN)) is Decision.CHECK


def test_check_not_found_archives():
    assert _decide(_marked(UNKNOWN), change_found=False) is Decision.ARCHIVE


def test_check_found_without_record_detaches():
    assert _decide(_marked(UNKNOWN), change_found=True) is Decision.DETACH


def test_a_detached_doc_with_a_gone_mark_archives_and_an_archived_one_clears():
    assert _decide(replace(_marked(GONE), state=MIRROR_DETACHED)) is Decision.ARCHIVE
    assert _decide(replace(_marked(GONE), state=MIRROR_ARCHIVED)) is Decision.CLEAR


def test_a_mark_without_ready_at_uses_gone_at_plus_grace():
    doc = replace(_marked(), mark_ready_at=None)
    assert _decide(doc, now=T + timedelta(minutes=5)) is Decision.WAIT
    assert _decide(doc) is Decision.ARCHIVE


# -- the root ------------------------------------------------------------------------------------------------------

def _root(gone=T):
    return MirrorRoot(user_id="u", drive_identity="d", root_item_id="r", root_name="_Transcripts", root_gone_at=gone)


LATE = T + MIRROR_ROOT_GRACE + timedelta(minutes=1)


def _rd(root, **kw):
    args = dict(record_present=False, round_complete=True, now=LATE)
    args.update(kw)
    return root_decision(root, **args)


def test_root_waits_for_round_and_24h():
    assert _rd(_root(), round_complete=False) is RootDecision.WAIT
    assert _rd(_root(), now=T + timedelta(hours=23)) is RootDecision.WAIT
    assert _rd(_root()) is RootDecision.CHECK


def test_root_record_back_clears():
    assert _rd(_root(), record_present=True, now=T) is RootDecision.CLEAR


def test_root_found_clears():
    assert _rd(_root(), change_found=True) is RootDecision.CLEAR


def test_root_not_found_is_lost():
    assert _rd(_root(), change_found=False) is RootDecision.LOST


def test_root_without_a_mark_clears():
    assert _rd(_root(None)) is RootDecision.CLEAR


# -- failures ------------------------------------------------------------------------------------------------------

def test_charge_backoff_and_park_at_5():
    doc, rec = _doc(), mirror_plan(_rec())
    for n in range(1, 5):
        doc, held = charged(doc, rec, T)
        assert doc.attempts == n and doc.retry_at == T + backoff(n) and doc.failed_at is None
        assert held.mirror_retry_at == doc.retry_at and held.mirror_due is False
    doc, held = charged(doc, rec, T)
    assert doc.attempts == 5 and doc.failed_at == T
    assert held.mirror_retry_at == T + MIRROR_FAILED_RETRY and held.mirror_held_digest == mirror_digest(rec)


def test_parked_retried_on_new_digest_or_after_7_days():
    parked = _doc(attempts=5, failed_at=T, retry_at=T + timedelta(hours=1), failed_digest="d1")
    assert retry_due(parked, now=T + timedelta(days=6), digest="") is False
    assert retry_due(parked, now=T + MIRROR_FAILED_RETRY, digest="") is True
    assert retry_due(parked, now=T + timedelta(days=1), digest="other") is True
    assert retry_due(parked, now=T + timedelta(days=1), digest="d1") is False


def test_backing_off_waits_for_retry_at_and_a_clean_doc_is_due():
    doc = _doc(attempts=2, retry_at=T + timedelta(minutes=20))
    assert retry_due(doc, now=T + timedelta(minutes=19), digest="") is False
    assert retry_due(doc, now=T + timedelta(minutes=20), digest="") is True
    assert retry_due(_doc(), now=T, digest="") is True


def test_reset_charges():
    out = reset_charges(_doc(attempts=5, failed_at=T, retry_at=T))
    assert (out.attempts, out.failed_at, out.retry_at) == (0, None, None)


# -- prune ---------------------------------------------------------------------------------------------------------

def test_prune_only_without_children_and_source():
    folder = _doc(kind=KIND_FOLDER)
    assert prune_due(folder, source_present=False, has_child_docs=False) is True
    assert prune_due(folder, source_present=True, has_child_docs=False) is False
    assert prune_due(folder, source_present=False, has_child_docs=True) is False
    assert prune_due(_doc(kind=KIND_ARCHIVE), source_present=False, has_child_docs=False) is False
    assert prune_due(_doc(), source_present=False, has_child_docs=False) is False
    assert prune_due(replace(folder, state=MIRROR_ARCHIVED), source_present=False, has_child_docs=False) is False
    assert folder.state == MIRROR_ACTIVE


# -- the archive day -----------------------------------------------------------------------------------------------

def test_local_day_uses_the_user_zone_else_utc():
    late = datetime(2026, 10, 21, 23, 30, tzinfo=timezone.utc)
    assert local_day(late, "Europe/Madrid") == "2026-10-22"
    assert local_day(late, "") == "2026-10-21"
    assert local_day(late, "Not/AZone") == "2026-10-21"
    assert local_day(datetime(2026, 10, 21, 23, 30), "Europe/Madrid") == "2026-10-22"     # naive = UTC


# -- fix round 1 ---------------------------------------------------------------------------------------------------

def test_create_when_the_doc_holds_only_an_intent():
    intent = _doc(mirror_item_id="", pending_op="create", pending_name="Interview.mp4.md", pending_parent_id="P", pending_at=T)
    assert file_action(_rec(), intent, "h1", _place()) is Action.CREATE
    assert file_action(_rec(), replace(intent, state=MIRROR_ARCHIVED), "h1", _place()) is Action.CREATE
    assert file_action(_rec(), replace(intent, state=MIRROR_DETACHED), "h1", _place()) is Action.CREATE
    folder_intent = _doc(kind=KIND_FOLDER, source_item_id="f1", source_name="Videos", source_parent_id="p0",
                         mirror_name="Videos", mirror_item_id="", pending_op="create")
    assert folder_action(_folder(), folder_intent, _place("Videos")) is Action.CREATE
    assert folder_action(_folder(), replace(folder_intent, mirror_item_id="m1"), _place("Videos")) is Action.STAMP_ONLY


def test_charged_records_the_failed_digest_and_a_new_digest_is_told_from_a_wakeup():
    rec = mirror_plan(_rec())
    doc, _ = charged(_doc(), rec, T)
    assert doc.failed_digest == mirror_digest(rec)
    assert digest_changed(doc, mirror_digest(rec)) is False
    assert digest_changed(doc, mirror_digest(replace(rec, name="X.mp4"))) is True
    assert digest_changed(_doc(), "anything") is False          # never failed: nothing to be new against
    assert reset_charges(doc, mirror_digest(rec)) == doc        # woken at the same digest: the budget stays
    fresh = reset_charges(doc, mirror_digest(replace(rec, name="X.mp4")))
    assert (fresh.attempts, fresh.failed_digest, fresh.retry_at) == (0, "", None)
    assert reset_charges(doc).attempts == 0                      # a success resets unconditionally


def test_charged_doc_is_doc_only_and_charged_reuses_it():
    doc = charged_doc(_doc(), T)
    assert (doc.attempts, doc.retry_at, doc.failed_digest) == (1, T + backoff(1), "")
    full, _ = charged(_doc(), mirror_plan(_rec()), T)
    assert replace(full, failed_digest="") == doc


def test_marked_twice_keeps_earliest_gone_at_latest_ready_and_upgrades_why():
    first = marked(_doc(), DeletionNotice("i1", UNSTORED, T))
    later = T + timedelta(hours=3)
    again = marked(first, DeletionNotice("i1", GONE, later))
    assert again.gone_at == T and again.gone_why == GONE and again.mark_ready_at == later + MIRROR_ARCHIVE_GRACE
    earlier = marked(again, DeletionNotice("i1", UNKNOWN, T - timedelta(hours=1)))
    assert earlier.gone_at == T - timedelta(hours=1) and earlier.gone_why == GONE
    assert earlier.mark_ready_at == later + MIRROR_ARCHIVE_GRACE


@pytest.mark.parametrize("state", ["deleting", "unresolved"])
def test_folder_in_a_dead_state_is_not_eligible(state):
    folder = replace(_folder(), mirrored_digest="old", state=RecordState(state))
    assert mirror_eligible(folder) is False and mirror_plan(folder).mirror_due is False

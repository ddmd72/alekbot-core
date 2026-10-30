"""Files the bot cannot open (USER_DRIVE_INDEXING_RFC §26.2, D33, Δ47, Δ51, Δ52). Pure planner decisions: the
`inaccessible` fields read as a state, no content or enrichment work while they are set, the retry schedule (an
observation of the item or its parent at most once per INACCESSIBLE_RETRY_MIN, otherwise INACCESSIBLE_RETRY_SLOW), and
a regained item back on the ordinary content path."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from src.domain.connection_scope import ConnectionScope
from src.domain import drive_enrich_plan as enrich
from src.domain.drive_card import card_hash
from src.domain.drive_index import (
    DriveChange, DriveIndexRecord, EpochPhase, Priority, RecordState, inaccessible,
)
from src.domain.drive_index_params import INACCESSIBLE_RETRY_MIN, INACCESSIBLE_RETRY_SLOW
from src.domain.drive_index_planner import (
    PlanContext, access_observed, access_recheck_at, access_recheck_due, access_regained, access_rechecked,
    content_due, marked_inaccessible, plan_observation, schedule, triage_due,
)

NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
ROOT = "root"


def ctx(now=NOW):
    return PlanContext(scope=ConnectionScope(user_id="u", connection_id="u"), root_item_id=ROOT, epoch=1, phase=EpochPhase.IDLE, now=now,
                       place_of=lambda lat, lon: None)


def change(item_id="f1", name="scan.pdf", **kw):
    base = dict(item_id=item_id, name=name, parent_id=ROOT, size_bytes=1000, mime_type="application/pdf",
                content_version="c1", modified_at=NOW - timedelta(days=3), fs_modified_at=NOW - timedelta(days=3))
    return DriveChange(**{**base, **kw})


def pending_file(**kw):
    """A first-seen file, carded already, so the only work a marking leaves is the re-read."""
    rec = plan_observation(change(**kw), None, None, None, ctx()).upsert
    assert rec is not None and rec.state is RecordState.PENDING
    return replace(rec, has_card=True, card_hash=card_hash(rec))


def test_marking_is_a_state_read_from_two_fields_not_a_record_state():
    rec = marked_inaccessible(pending_file(), NOW)
    assert inaccessible(rec) and rec.inaccessible_since == NOW
    assert rec.state is RecordState.PENDING          # the content outcome is kept (Δ47)
    assert rec.inaccessible_notified_since is None   # the fan-out's field: never written here (Δ54)


def test_a_marked_file_has_no_content_or_triage_work():
    assert content_due(pending_file())
    rec = marked_inaccessible(pending_file(), NOW)
    assert not content_due(rec) and not content_due(rec, NOW)
    image = pending_file(item_id="i1", name="IMG_1.jpg", mime_type="image/jpeg")
    assert triage_due(image) and not triage_due(marked_inaccessible(image, NOW))


def test_a_run_ends_without_an_attempt():
    started = replace(pending_file(), attempt_started_at=NOW, attempts=1, defer_until=NOW + timedelta(minutes=30),
                      defer_priority=int(Priority.RETRY))
    rec = marked_inaccessible(started, NOW)
    assert (rec.attempt_started_at, rec.defer_until, rec.defer_priority, rec.attempts) == (None, None, None, 1)
    assert rec.state is RecordState.PENDING and rec.reason is None


def test_marking_again_keeps_the_first_since():
    first = marked_inaccessible(pending_file(), NOW)
    again = marked_inaccessible(first, NOW + timedelta(days=2))
    assert again.inaccessible_since == NOW and again.inaccessible_checked_at == NOW + timedelta(days=2)


def test_without_an_observation_the_retry_is_the_slow_one():
    rec = marked_inaccessible(pending_file(), NOW)
    assert access_recheck_at(rec) == NOW + INACCESSIBLE_RETRY_SLOW
    assert rec.due_at == NOW + INACCESSIBLE_RETRY_SLOW and rec.priority == int(Priority.MAINTENANCE)
    assert not access_recheck_due(rec, NOW + INACCESSIBLE_RETRY_SLOW - timedelta(seconds=1))
    assert access_recheck_due(rec, NOW + INACCESSIBLE_RETRY_SLOW)


def test_an_observation_makes_it_due_at_most_once_per_retry_min():
    rec = marked_inaccessible(pending_file(), NOW)
    seen = access_observed(rec, NOW + timedelta(minutes=5))
    assert seen.inaccessible_reobserved
    assert access_recheck_at(seen) == NOW + INACCESSIBLE_RETRY_MIN and seen.due_at == NOW + INACCESSIBLE_RETRY_MIN
    later = access_observed(rec, NOW + timedelta(hours=3))
    assert later.due_at == NOW + INACCESSIBLE_RETRY_MIN   # past the cap: due at once (a past due_at)


def test_a_failed_recheck_resets_the_clock_and_the_observation():
    seen = access_observed(marked_inaccessible(pending_file(), NOW), NOW)
    checked = access_rechecked(seen, NOW + timedelta(hours=2))
    assert checked.inaccessible_since == NOW and checked.inaccessible_checked_at == NOW + timedelta(hours=2)
    assert not checked.inaccessible_reobserved
    assert checked.due_at == NOW + timedelta(hours=2) + INACCESSIBLE_RETRY_SLOW


def test_delta_re_emitting_an_unchanged_marked_item_flags_the_observation_once():
    rec = marked_inaccessible(pending_file(), NOW)
    plan = plan_observation(change(), rec, None, None, ctx(NOW + timedelta(minutes=20)))
    assert plan.upsert is not None and plan.upsert.inaccessible_reobserved
    assert plan.upsert.due_at == NOW + INACCESSIBLE_RETRY_MIN
    again = plan_observation(change(), plan.upsert, None, None, ctx(NOW + timedelta(minutes=30)))
    assert again.upsert is None   # already flagged: no write per re-emission


def test_a_re_plan_of_a_stored_record_is_not_an_observation():
    rec = marked_inaccessible(pending_file(), NOW)
    from src.domain.drive_index_planner import change_from_record
    plan = plan_observation(change_from_record(rec), rec, None, None, ctx(), observed=False)
    assert plan.upsert is None or not plan.upsert.inaccessible_reobserved


def test_an_edit_seen_while_marked_keeps_the_marking():
    rec = marked_inaccessible(pending_file(), NOW)
    plan = plan_observation(change(content_version="c2", modified_at=NOW), rec, None, None, ctx())
    assert plan.upsert is not None and inaccessible(plan.upsert) and not content_due(plan.upsert)
    assert plan.upsert.inaccessible_since == NOW


def test_a_tombstone_still_deletes_a_marked_file():
    rec = marked_inaccessible(pending_file(), NOW)
    plan = plan_observation(DriveChange(item_id=rec.item_id, deleted=True), rec, None, None, ctx())
    assert plan.delete_item_id == rec.item_id and plan.upsert is None


def test_access_regained_clears_the_tick_fields_and_the_content_is_due_again():
    rec = access_observed(marked_inaccessible(pending_file(), NOW), NOW)
    back = access_regained(rec, NOW + timedelta(hours=1))
    assert not inaccessible(back)
    assert (back.inaccessible_checked_at, back.inaccessible_reobserved) == (None, False)
    assert content_due(back) and back.due_at == NOW + timedelta(hours=1)


def test_a_readable_generation_stays_readable_while_marked():
    indexed = replace(pending_file(), state=RecordState.INDEXED, indexed_generation=2, indexed_version="c0",
                      content_version="c1")
    rec = marked_inaccessible(indexed, NOW)
    assert rec.indexed_generation == 2 and rec.stale


def test_no_enrichment_job_is_ready_while_marked_and_it_is_no_describe_gap():
    indexed = replace(pending_file(), state=RecordState.INDEXED, indexed_generation=2, indexed_version="c1",
                      chunk_count=3, text_chars=2000)
    open_plan = enrich.planned(schedule(indexed, NOW, first_seen=False), enrich.default_context(NOW))
    assert open_plan.profile_state == "ready"   # the control: readable, it would be profiled
    closed = enrich.planned(marked_inaccessible(indexed, NOW), enrich.default_context(NOW))
    assert closed.profile_state != "ready" and closed.passages_state != "ready" and not closed.enrich_ready
    assert closed.describe_gap == ""
    assert enrich.profile_settled(closed, enrich.default_context(NOW))
    assert enrich.passages_settled(closed, enrich.default_context(NOW))


def test_unmarked_records_have_no_recheck():
    rec = pending_file()
    assert access_recheck_at(rec) is None and not access_recheck_due(rec, NOW + timedelta(days=30))
    assert isinstance(rec, DriveIndexRecord)

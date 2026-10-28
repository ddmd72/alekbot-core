"""`content_requeued` (the owner's requeue of a failed content read, USER_DRIVE_INDEXING_RFC §8.2, §3.4): a failed
outcome stands until the content or the extraction rules change; the requeue makes it due again through the planner's
own tests (`content_due`, `quiet_over`, `schedule`) and keeps everything else. Pure."""
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone

import pytest

from src.domain import drive_index_policy as policy
from src.domain.drive_card import card_hash
from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import DriveChange, EpochPhase, Priority, Reason, RecordState
from src.domain.drive_index_params import MAX_ATTEMPTS
from src.domain.drive_index_planner import (
    FAILED_REASONS, PlanContext, capped, content_due, content_requeued, content_retry, demoted, generation_started,
    indexed, plan_observation, quiet_over, reextraction_due, with_card,
)
from src.domain.drive_index_policy import SLICE2_CONTENT_KINDS, SLICE2_TRIAGED_KINDS

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
LATER = NOW + timedelta(days=3)
ROOT = "root"
SPACE = "test/8"

pytestmark = pytest.mark.usefixtures("admitted")


@pytest.fixture
def admitted(monkeypatch):
    monkeypatch.setattr(policy, "ADMITTED_KINDS", SLICE2_CONTENT_KINDS)
    monkeypatch.setattr(policy, "TRIAGED_KINDS", SLICE2_TRIAGED_KINDS)


def ctx(now=NOW):
    return PlanContext(user_id="user-1", root_item_id=ROOT, epoch=1, phase=EpochPhase.IDLE, now=now,
                       place_of=lambda lat, lon: None)


def ch(name="birth-certificate.pdf", cv="c1"):
    return DriveChange(item_id="item-1", name=name, parent_id=ROOT, size_bytes=1000, content_version=cv,
                       modified_at=NOW - timedelta(days=1), fs_modified_at=NOW - timedelta(days=30))


def observe(change, existing=None, now=NOW):
    return plan_observation(change, existing, None, None, ctx(now)).upsert


def carded(rec, now=NOW):
    return with_card(rec, card_hash(rec), SPACE, now)


def failed(rec, reason=Reason.EXTRACT_ERROR):
    """The tick's own path to `failed`: a run started and failed MAX_ATTEMPTS times."""
    return content_retry(replace(generation_started(rec, NOW), attempts=MAX_ATTEMPTS - 1), reason, NOW)


def indexed_rec():
    run = generation_started(carded(observe(ch())), NOW)
    outcome = ExtractionOutcome(status=ExtractionStatus.TEXT, text="Clause one. " * 300, pages_total=3,
                                extracted_by="parser@1")
    return carded(indexed(run, outcome=outcome, generation=1, chunk_count=4, now=NOW))


def failed_after_edit(reason=Reason.EXTRACT_ERROR):
    """An edit (c2) whose read failed: the readable generation of c1 is kept, served as stale."""
    return failed(replace(indexed_rec(), state=RecordState.PENDING, content_version="c2"), reason)


def failed_same_version():
    """Re-read under new extraction rules at the SAME content version, then failed: its readable generation is of
    this version, so only `extraction_version` 0 keeps it from counting as indexed."""
    return failed(replace(reextraction_due(indexed_rec()), state=RecordState.PENDING))


def test_a_failed_outcome_stands_without_the_requeue():
    rec = failed_after_edit()
    assert (rec.state, rec.reason, rec.due_at) == (RecordState.FAILED, Reason.EXTRACT_ERROR, None)
    assert not content_due(rec, LATER)
    renamed = observe(ch("id_card_scan.pdf", cv="c2"), rec, now=LATER)
    assert renamed.state is RecordState.FAILED and not content_due(renamed, LATER)


@pytest.mark.parametrize("reason", sorted(FAILED_REASONS, key=lambda r: r.value))
def test_requeued_is_due_now_for_a_fresh_read_in_class_3(reason):
    rec = failed_after_edit(reason)
    out = content_requeued(rec, LATER)
    assert content_due(out, LATER) and quiet_over(out, LATER)
    assert (out.due_at, out.priority) == (LATER, int(Priority.MAINTENANCE))
    assert (out.state, out.reason, out.attempts, out.extraction_version) == (RecordState.PENDING, None, 0, 0)


def test_requeued_keeps_the_card_the_chunks_and_every_other_field():
    rec = failed_after_edit()
    out = content_requeued(rec, LATER)
    moved = {"state", "reason", "extraction_version", "attempts", "attempt_started_at", "dirty_since",
             "defer_until", "defer_priority", "model_outages", "due_at", "priority"}
    for f in fields(rec):
        if f.name not in moved:
            assert getattr(out, f.name) == getattr(rec, f.name), f.name
    assert out.has_card and out.indexed_generation == 1 and out.content_version == "c2"


def test_a_readable_generation_of_the_same_version_does_not_count_as_indexed():
    rec = failed_same_version()
    assert rec.indexed_version == rec.content_version and rec.indexed_generation == 1
    out = content_requeued(rec, LATER)
    assert content_due(out, LATER) and out.due_at == LATER


def test_a_re_observation_keeps_the_requeued_record_pending():
    out = content_requeued(failed_after_edit(), LATER)
    renamed = observe(ch("id_card_scan.pdf", cv="c2"), out, now=LATER)
    assert renamed.state is RecordState.PENDING and content_due(renamed, LATER)


def test_attempts_quiet_period_deferral_and_outages_are_cleared():
    rec = replace(failed_after_edit(), attempts=3, attempt_started_at=NOW, dirty_since=NOW, model_outages=2,
                  defer_until=LATER + timedelta(days=30), defer_priority=int(Priority.RETRY))
    out = content_requeued(rec, LATER)
    assert (out.attempts, out.attempt_started_at, out.dirty_since, out.model_outages) == (0, None, None, 0)
    assert (out.defer_until, out.defer_priority, out.due_at) == (None, None, LATER)


def test_a_file_without_a_card_is_due_in_class_3_too():
    rec = failed(observe(ch()))
    assert not rec.has_card
    out = content_requeued(rec, LATER)
    assert content_due(out, LATER) and (out.due_at, out.priority) == (LATER, int(Priority.MAINTENANCE))


def test_a_card_capped_file_is_pending_but_holds_its_read_until_released():
    out = content_requeued(capped(failed_after_edit(), NOW), LATER)
    assert out.state is RecordState.PENDING and out.card_capped
    assert not content_due(out, LATER) and (out.due_at, out.priority) == (None, None)


@pytest.mark.parametrize("make", [
    lambda: indexed_rec(),
    lambda: replace(indexed_rec(), state=RecordState.PENDING, content_version="c2"),
    lambda: demoted(carded(observe(ch())), Reason.NO_TEXT, NOW),
    lambda: replace(failed_after_edit(), reason=None),
], ids=["indexed", "pending", "metadata_only", "failed_without_a_per_file_reason"])
def test_any_other_record_is_refused(make):
    with pytest.raises(ValueError):
        content_requeued(make(), LATER)

"""`rereadable` / `content_reread` (the owner's re-read of an outcome a later fix improves, USER_DRIVE_INDEXING_RFC
§5.4, §24.5, §8.2): a truncated read and a text image the reader got no text from stand like any outcome; the re-read
makes them due again through the planner's own tests (`content_due`, `triage_due`, `quiet_over`, `schedule`) and keeps
everything else. Pure."""
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.connection_scope import ConnectionScope
from src.domain import drive_index_policy as policy
from src.domain.drive_card import card_hash
from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus, TriageResult
from src.domain.drive_index import DriveChange, EpochPhase, Priority, Reason, RecordState, TriageVerdict
from src.domain.drive_index_planner import (
    PlanContext, capped, content_due, content_reread, demoted, generation_started, indexed, plan_observation,
    quiet_over, rereadable, triage_due, triaged, with_card,
)
from src.domain.drive_index_policy import SLICE2_CONTENT_KINDS, SLICE2_TRIAGED_KINDS

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
LATER = NOW + timedelta(days=3)
ROOT = "root"
SPACE = "test/8"
# what the re-read moves; every other field is kept
MOVED = {"state", "reason", "extraction_version", "attempts", "attempt_started_at", "dirty_since", "defer_until",
         "defer_priority", "model_outages", "due_at", "priority"}

pytestmark = pytest.mark.usefixtures("admitted")


@pytest.fixture
def admitted(monkeypatch):
    monkeypatch.setattr(policy, "ADMITTED_KINDS", SLICE2_CONTENT_KINDS)
    monkeypatch.setattr(policy, "TRIAGED_KINDS", SLICE2_TRIAGED_KINDS)


def ctx(now=NOW):
    return PlanContext(scope=ConnectionScope(user_id="user-1", connection_id="user-1"), root_item_id=ROOT, epoch=1, phase=EpochPhase.IDLE, now=now,
                       place_of=lambda lat, lon: None)


def ch(name="birth-certificate.pdf", cv="c1", item_id="item-1"):
    return DriveChange(item_id=item_id, name=name, parent_id=ROOT, size_bytes=1000, content_version=cv,
                       modified_at=NOW - timedelta(days=1), fs_modified_at=NOW - timedelta(days=30))


def observe(change, existing=None, now=NOW):
    return plan_observation(change, existing, None, None, ctx(now)).upsert


def carded(rec, now=NOW):
    return with_card(rec, card_hash(rec), SPACE, now)


def indexed_rec(truncated=True):
    """Jamie's birth certificate, read by the parser and cut by the chunk cap of its day."""
    run = generation_started(carded(observe(ch())), NOW)
    outcome = ExtractionOutcome(status=ExtractionStatus.TEXT, text="Clause one. " * 300, pages_total=3,
                                extracted_by="parser@1", truncated=truncated)
    return carded(indexed(run, outcome=outcome, generation=1, chunk_count=4, now=NOW))


def text_image(verdict=TriageVerdict.TEXT):
    """A scan of the garage receipt, triaged as text."""
    rec = carded(observe(ch("garage-receipt.png", item_id="item-2")))
    return triaged(rec, TriageResult(item_id=rec.item_id, verdict=verdict), NOW)


def no_text_image():
    """The tick's own path: triaged as text, its run started, then demoted no_text (no rendition the reader could
    take, or a reader that read nothing)."""
    return carded(demoted(generation_started(text_image(), NOW), Reason.NO_TEXT, NOW))


# -- an indexed, truncated read -------------------------------------------------------------------------------------

def test_a_truncated_read_stands_without_the_re_read():
    rec = indexed_rec()
    assert (rec.state, rec.truncated, rec.due_at) == (RecordState.INDEXED, True, None)
    assert not content_due(rec, LATER)
    assert observe(ch("birth-certificate (copy).pdf"), rec, now=LATER).state is RecordState.INDEXED


def test_a_truncated_read_is_due_now_for_a_fresh_read_in_class_3():
    out = content_reread(indexed_rec(), LATER)
    assert rereadable(indexed_rec())
    assert content_due(out, LATER) and quiet_over(out, LATER) and not triage_due(out, LATER)
    assert (out.due_at, out.priority) == (LATER, int(Priority.MAINTENANCE))
    assert (out.state, out.reason, out.attempts, out.extraction_version) == (RecordState.PENDING, None, 0, 0)


def test_a_truncated_read_keeps_its_card_its_chunks_and_every_other_field():
    rec = indexed_rec()
    out = content_reread(rec, LATER)
    for f in fields(rec):
        if f.name not in MOVED:
            assert getattr(out, f.name) == getattr(rec, f.name), f.name
    assert out.has_card and out.indexed_generation == 1 and out.chunk_count == 4 and out.truncated


def test_a_re_observation_keeps_the_re_read_pending():
    out = content_reread(indexed_rec(), LATER)
    renamed = observe(ch("birth-certificate (copy).pdf"), out, now=LATER)
    assert renamed.state is RecordState.PENDING and content_due(renamed, LATER)


def test_the_next_read_replaces_the_truncated_one():
    out = generation_started(content_reread(indexed_rec(), LATER), LATER)
    full = ExtractionOutcome(status=ExtractionStatus.TEXT, text="Clause one. " * 900, extracted_by="parser@1")
    done = indexed(out, outcome=full, generation=out.index_generation, chunk_count=9, now=LATER)
    assert (done.state, done.truncated, done.chunk_count, done.indexed_generation) == \
        (RecordState.INDEXED, False, 9, out.index_generation)
    assert not content_due(done, LATER) and not rereadable(done)


# -- a text image with no text --------------------------------------------------------------------------------------

def test_a_no_text_image_stands_without_the_re_read():
    rec = no_text_image()
    assert (rec.state, rec.reason, rec.triage_verdict) == (RecordState.METADATA_ONLY, Reason.NO_TEXT,
                                                            TriageVerdict.TEXT)
    assert not content_due(rec, LATER) and not triage_due(rec, LATER)


def test_a_no_text_image_goes_to_the_reader_with_its_verdict_kept():
    rec = no_text_image()
    out = content_reread(rec, LATER)
    assert content_due(out, LATER) and quiet_over(out, LATER)
    assert not triage_due(out, LATER)   # no second triage: the verdict holds
    assert (out.due_at, out.priority) == (LATER, int(Priority.MAINTENANCE))
    assert (out.state, out.reason, out.extraction_version) == (RecordState.PENDING, None, 0)
    for f in fields(rec):
        if f.name not in MOVED:
            assert getattr(out, f.name) == getattr(rec, f.name), f.name
    assert (out.triage_verdict, out.triaged_content_version) == (TriageVerdict.TEXT, "c1") and out.has_card


def test_a_card_capped_image_is_pending_but_holds_its_read_until_released():
    out = content_reread(capped(no_text_image(), NOW), LATER)
    assert out.state is RecordState.PENDING and out.card_capped
    assert not content_due(out, LATER) and (out.due_at, out.priority) == (None, None)


# -- what is not selected -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("make", [
    lambda: indexed_rec(truncated=False),
    lambda: content_reread(indexed_rec(), LATER),                         # already pending again
    lambda: replace(indexed_rec(), state=RecordState.PENDING, content_version="c2"),   # an edit waiting
    lambda: demoted(carded(observe(ch())), Reason.NO_TEXT, NOW),          # a pdf with no text: no image
    lambda: replace(no_text_image(), triage_version=0),                   # a verdict that no longer holds
    lambda: text_image(TriageVerdict.PHOTO),                              # a photograph
    lambda: demoted(generation_started(text_image(), NOW), Reason.TOO_LARGE, NOW),
    lambda: replace(indexed_rec(), kind="audio"),                         # a transcript: not the index step's read
    lambda: replace(indexed_rec(), is_folder=True),
], ids=["indexed_in_full", "already_pending", "pending_edit", "pdf_no_text", "stale_verdict", "photograph",
        "image_too_large", "transcribed", "folder"])
def test_any_other_record_is_not_rereadable_and_is_refused(make):
    rec = make()
    assert not rereadable(rec)
    with pytest.raises(ValueError):
        content_reread(rec, LATER)

"""Planner fixes from adversarial review B (USER_DRIVE_INDEXING_RFC §3.4, §7.3, §8.2, §12.3, §24.5, §24.7). Pure."""
from dataclasses import replace
from datetime import timedelta

import pytest

from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import Priority, Reason, RecordState, TriageVerdict
from src.domain.drive_index_params import MAX_ATTEMPTS, MODEL_EXTRACT_MAX_PAGES, MODEL_EXTRACT_PAGES_PER_MONTH
from src.domain.drive_index_planner import (
    chunks_reembedded, content_due, content_retry, deferred, demoted, generation_started, indexed, plan_observation,
    reembed_in_space, stale_versions, triage_due,
)
from src.domain.drive_index_run import no_rendition, reader_pages

from .test_drive_index_planner_content import (  # noqa: F401  (fixture)
    NOW, SPACE, admitted, carded, ch, ctx, image, indexed_rec, observe, stored, text,
)

pytestmark = pytest.mark.usefixtures("admitted")


# -- B-M2: a rename away and back reads the file again ----------------------------------------------------------------

def test_metadata_only_from_metadata_drops_the_extraction_stamp():
    rec = indexed_rec()
    away = observe(ch("contract.pdf.bak"), rec)
    assert (away.state, away.reason) == (RecordState.METADATA_ONLY, Reason.KIND_NOT_ADMITTED)
    assert (away.extraction_version, away.extracted_by) == (0, None)


def test_a_rename_away_and_back_makes_the_content_due_again():
    rec = indexed_rec()
    back = observe(ch("contract.pdf"), observe(ch("contract.pdf.bak"), rec))
    assert back.state is RecordState.PENDING and content_due(back)


def test_a_content_demotion_still_stands_over_a_rename():
    rec = carded(demoted(generation_started(stored(ch()), NOW), Reason.NO_TEXT, NOW,
                         outcome=ExtractionOutcome(status=ExtractionStatus.NO_TEXT, extracted_by="parser@1")))
    renamed = observe(ch("renamed.pdf"), rec)
    assert (renamed.state, renamed.reason, renamed.extracted_by) == (RecordState.METADATA_ONLY, Reason.NO_TEXT,
                                                                      "parser@1")


# -- minors: a nudge, leaving pending, the pre-run class ---------------------------------------------------------------

def test_a_nudged_new_version_restarts_attempts_and_ends_the_deferral():
    retry = content_retry(generation_started(stored(ch()), NOW), Reason.EXTRACT_ERROR, NOW)
    assert retry.attempts == 1 and retry.defer_until is not None
    nudged = plan_observation(ch(cv="c2"), retry, None, None, ctx(), nudged=True).upsert
    assert (nudged.attempts, nudged.defer_until, nudged.defer_priority) == (0, None, None)
    assert nudged.due_at == NOW and content_due(nudged, NOW)


def test_a_nudge_of_the_same_version_keeps_the_backoff():
    retry = content_retry(generation_started(stored(ch()), NOW), Reason.EXTRACT_ERROR, NOW)
    nudged = plan_observation(ch(), retry, None, None, ctx(), nudged=True).upsert or retry
    assert (nudged.attempts, nudged.defer_until) == (1, retry.defer_until)


def test_leaving_pending_clears_the_started_attempt():
    run = generation_started(stored(ch()), NOW)
    assert run.attempt_started_at is not None
    away = observe(ch("contract.pdf.bak"), run)
    assert away.state is RecordState.METADATA_ONLY and away.attempt_started_at is None


def test_deferred_keeps_the_class_the_run_started_from():
    edit = observe(ch(cv="c2", modified=NOW - timedelta(hours=2)), indexed_rec(), now=NOW)
    assert edit.priority == int(Priority.EDIT)
    run = replace(generation_started(edit, NOW), attempt_started_at=None)
    assert run.priority == int(Priority.RETRY)
    held = deferred(run, NOW + timedelta(minutes=5), NOW, priority=edit.priority)
    assert (held.priority, held.defer_priority) == (int(Priority.EDIT), int(Priority.EDIT))


# -- minor: an image with no rendition is retried before it is a photograph ---------------------------------------------

def test_no_rendition_backs_off_then_is_a_photograph_without_caption():
    rec = observe(image())
    assert triage_due(rec)
    for attempt in range(1, MAX_ATTEMPTS):
        rec = no_rendition(rec, NOW)
        assert rec.attempts == attempt and rec.triage_verdict is None and not triage_due(rec, NOW)
        assert rec.defer_until is not None and rec.defer_priority == int(Priority.RETRY)
    rec = no_rendition(rec, NOW)
    assert (rec.state, rec.reason, rec.triage_verdict, rec.caption) == (
        RecordState.METADATA_ONLY, Reason.PHOTOGRAPH, TriageVerdict.PHOTO, "")


# -- B-M4: pages a file needs from the month's reader budget -------------------------------------------------------------

def test_reader_pages_is_one_for_an_image():
    assert reader_pages("image", None) == 1


def test_reader_pages_is_the_parsers_page_count_for_a_pdf():
    parsed = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=37)
    assert reader_pages("pdf", parsed) == 37


def test_reader_pages_is_capped_by_the_per_file_maximum():
    parsed = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=MODEL_EXTRACT_MAX_PAGES * 3)
    assert reader_pages("pdf", parsed) == min(MODEL_EXTRACT_MAX_PAGES, MODEL_EXTRACT_PAGES_PER_MONTH)


def test_reader_pages_of_an_uncounted_pdf_is_the_most_the_reader_sends():
    parsed = ExtractionOutcome(status=ExtractionStatus.ERROR, pages_total=0)
    assert reader_pages("pdf", parsed) == min(MODEL_EXTRACT_MAX_PAGES, MODEL_EXTRACT_PAGES_PER_MONTH)


def test_reader_pages_never_exceeds_the_whole_monthly_budget(monkeypatch):
    from src.domain import drive_index_run
    monkeypatch.setattr(drive_index_run, "MODEL_EXTRACT_PAGES_PER_MONTH", 50)
    parsed = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=120)
    assert reader_pages("pdf", parsed) == 50


# -- B-M6: the chunks' embedding space ---------------------------------------------------------------------------------

def _indexed_in(space):
    run = generation_started(stored(ch()), NOW)
    return carded(indexed(run, outcome=text(), generation=run.index_generation, chunk_count=4, now=NOW,
                          chunk_space=space))


def test_indexed_stamps_the_chunk_space():
    assert _indexed_in(SPACE).chunk_space == SPACE


def test_chunks_in_another_space_are_stale():
    rec = _indexed_in("old/8")
    rec = replace(rec, embedding_space=SPACE)   # the card is already in the new space
    assert stale_versions(rec, embedding_space=SPACE)
    assert not stale_versions(_indexed_in(SPACE), embedding_space=SPACE)


def test_reembed_in_space_makes_the_chunks_due_without_reading_the_file():
    rec = replace(_indexed_in("old/8"), embedding_space=SPACE)
    due = reembed_in_space(rec, SPACE)
    assert due.chunk_reembed_pending and due.card_hash == rec.card_hash
    planned = plan_observation(ch(), due, None, None, ctx(), observed=False).upsert
    assert planned is not None and planned.chunk_reembed_pending and planned.due_at == NOW
    assert planned.state is RecordState.INDEXED and not content_due(planned)


def test_chunks_reembedded_commits_the_new_generation_in_the_new_space():
    rec = reembed_in_space(replace(_indexed_in("old/8"), embedding_space=SPACE), SPACE)
    done = chunks_reembedded(replace(rec, index_generation=rec.index_generation + 1), rec.index_generation + 1,
                             SPACE, NOW)
    assert (done.indexed_generation, done.chunk_space, done.chunk_reembed_pending) == (
        rec.index_generation + 1, SPACE, False)
    assert done.state is RecordState.INDEXED and done.due_at is None
    assert not stale_versions(done, embedding_space=SPACE)


def test_a_demotion_drops_the_chunk_space():
    rec = reembed_in_space(replace(_indexed_in("old/8"), embedding_space=SPACE), SPACE)
    away = observe(ch("contract.pdf.bak"), rec)
    assert (away.indexed_generation, away.chunk_space, away.chunk_reembed_pending) == (None, "", False)

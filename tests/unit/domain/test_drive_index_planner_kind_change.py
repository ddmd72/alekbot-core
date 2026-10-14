"""A content kind change resets the retry state, like a new version (arbiter R1-4; USER_DRIVE_INDEXING_RFC §7.3, §8.2,
§24.5). The attempts, backoff and deferral counted the old kind's work: a backed-off `.jpg` renamed to `x.pdf` is a
different extractor chain, so its first try must not wait out the image's backoff or start near MAX_ATTEMPTS. Pure."""
from datetime import timedelta

import pytest

from src.domain.drive_index import Priority, Reason, RecordState
from src.domain.drive_index_planner import (
    content_due, content_retry, deferred, generation_started, plan_observation, triage_due,
)
from src.domain.drive_index_run import no_rendition

from .test_drive_index_planner_content import (  # noqa: F401  (fixture)
    NOW, admitted, ch, ctx, image, observe, stored,
)

pytestmark = pytest.mark.usefixtures("admitted")


def _backed_off_image():
    """An image waiting for triage whose rendition was missing twice: attempts 2, held in the retry class."""
    rec = stored(image())
    assert triage_due(rec)
    rec = no_rendition(no_rendition(rec, NOW), NOW)
    assert rec.state is RecordState.PENDING and rec.attempts == 2
    assert rec.defer_until is not None and rec.defer_until > NOW
    assert rec.defer_priority == int(Priority.RETRY)
    return rec


def test_a_backed_off_jpg_renamed_to_pdf_starts_fresh():
    renamed = observe(ch("x.pdf", item_id="img", mime_type="application/pdf"), _backed_off_image())
    assert renamed.kind == "pdf" and renamed.state is RecordState.PENDING
    assert (renamed.attempts, renamed.attempt_started_at) == (0, None)
    assert (renamed.defer_until, renamed.defer_priority) == (None, None)
    assert content_due(renamed, NOW) and renamed.due_at == NOW
    assert renamed.priority != int(Priority.RETRY)


def test_a_started_pdf_run_renamed_to_an_image_starts_fresh():
    run = content_retry(generation_started(stored(ch()), NOW), Reason.EXTRACT_ERROR, NOW)
    run = generation_started(run, NOW)
    assert run.attempts == 1 and run.attempt_started_at is not None
    renamed = observe(ch("contract.jpg", mime_type="image/jpeg"), run)
    assert renamed.kind != run.kind and renamed.state is RecordState.PENDING
    assert (renamed.attempts, renamed.attempt_started_at, renamed.defer_until, renamed.defer_priority) == \
        (0, None, None, None)
    assert triage_due(renamed, NOW) and renamed.due_at == NOW


def test_a_deferred_record_whose_kind_changes_is_not_held_by_the_old_deferral():
    held = deferred(stored(ch()), NOW + timedelta(days=20), NOW, maintenance=True)   # a spent monthly budget
    renamed = observe(ch("contract.docx", mime_type=""), held)
    assert renamed.kind == "docx"
    assert (renamed.defer_until, renamed.defer_priority) == (None, None)
    assert content_due(renamed, NOW)


def test_a_rename_that_keeps_the_kind_keeps_the_backoff():
    rec = _backed_off_image()
    renamed = observe(ch("IMG_2.jpg", item_id="img", mime_type="image/jpeg"), rec)
    assert (renamed.attempts, renamed.defer_until, renamed.defer_priority) == \
        (rec.attempts, rec.defer_until, rec.defer_priority)


def test_the_kind_change_resets_like_a_new_version():
    rec = _backed_off_image()
    by_kind = observe(ch("x.pdf", item_id="img", mime_type="application/pdf"), rec)
    by_version = plan_observation(image(cv="c2"), rec, None, None, ctx(), nudged=True).upsert
    assert (by_kind.attempts, by_kind.defer_until, by_kind.defer_priority) == \
        (by_version.attempts, by_version.defer_until, by_version.defer_priority)

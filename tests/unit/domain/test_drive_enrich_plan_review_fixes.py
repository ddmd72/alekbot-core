"""drive_enrich_plan after review rv_sv2_p5 (USER_DRIVE_INDEXING_RFC §25.4.5): a due content run defers both jobs
with no wake time of their own (MINOR 1); a deferral's wake is only ever in the future; the unusable holds (M4)."""
from dataclasses import replace
from datetime import timedelta

from src.domain import drive_enrich_plan as ep
from src.domain.drive_enrichment import JobState
from src.domain.drive_index import PROFILE_VERSION, RecordState
from src.domain.drive_index_params import PROFILE_REFRESH_COOLDOWN

from tests.unit.domain.test_drive_enrich_plan import NOW, ctx, doc


def _edited(**kw):
    """Indexed at c1, now at c2: its content run is due (an edit observed, its quiet period running)."""
    return doc(state=RecordState.PENDING, content_version="c2", indexed_version="c1", **kw)


def test_a_due_content_run_defers_both_jobs_without_a_wake_time():
    rec = ep.planned(_edited(), ctx())
    assert (rec.profile_state, rec.passages_state) == (JobState.DEFERRED.value, JobState.DEFERRED.value)
    assert not rec.enrich_ready and rec.enrich_wake_at is None


def test_the_same_record_once_read_is_ready():
    rec = ep.planned(doc(indexed_version="c1"), ctx())
    assert (rec.profile_state, rec.passages_state) == (JobState.READY.value, JobState.READY.value)


def test_a_past_cooldown_end_is_no_wake():
    over = ep.planned(_edited(profile_defer_until=NOW - timedelta(hours=1)), ctx())
    assert over.profile_state == JobState.DEFERRED.value and over.enrich_wake_at is None
    running = ep.planned(_edited(profile_defer_until=NOW + PROFILE_REFRESH_COOLDOWN), ctx())
    assert running.enrich_wake_at is None or running.enrich_wake_at > NOW


def test_unusable_holds_are_for_the_current_version_and_input():
    rec = ep.planned(doc(indexed_version="c1"), ctx())
    held = ep.planned(ep.profile_unusable(rec), ctx())
    assert (held.profile_state, held.profile_hold) == (JobState.HELD.value, "failed")
    assert ep.planned(replace(held, profile_input_fingerprint="fp-2"), ctx()).profile_state == JobState.READY.value
    assert ep.planned(replace(held, profile_hold_version=f"{PROFILE_VERSION - 1}:fp-1"),
                      ctx()).profile_state == JobState.READY.value
    gone = ep.planned(ep.passages_unusable(rec), ctx())
    assert (gone.passages_state, gone.passages_hold) == (JobState.HELD.value, "failed")
    assert ep.planned(replace(gone, indexed_generation=2), ctx()).passages_state == JobState.READY.value

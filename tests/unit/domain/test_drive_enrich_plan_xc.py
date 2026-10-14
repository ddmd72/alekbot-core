"""Search v2 cross-cutting review fixes in the enrichment plans (USER_DRIVE_INDEXING_RFC §25.4.5, §25.4.9, §25.5.6):
the coverage line's derived `describe_gap` (XC-3) and the refresh cooldowns kept through clears and template writes
(XC-6)."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.domain import drive_enrich_plan as ep
from src.domain.drive_enrichment import USD_CAPS, DriveProfile, EnrichContext, JobState, ProfileSource
from src.domain.drive_index import KEYS_VERSION, DriveIndexRecord, RecordState, Scope
from src.domain.drive_index_params import PASSAGE_REFRESH_COOLDOWN, PROFILE_MIN_CHARS, PROFILE_REFRESH_COOLDOWN

NOW = datetime(2026, 10, 20, 8, tzinfo=timezone.utc)
FP = "fp-1"
R, C, D, P, H, N = (JobState.READY.value, JobState.CURRENT.value, JobState.DEFERRED.value, JobState.PARKED.value,
                    JobState.HELD.value, JobState.NONE.value)


def ctx(now: datetime = NOW) -> EnrichContext:
    return EnrichContext(now=now, month_usd={}, caps_usd=dict(USD_CAPS), initial_balance_usd=40.0, initial_open=True)


def doc(**kw) -> DriveIndexRecord:
    base = dict(user_id="u", item_id="d1", parent_id="root", name="lease.pdf", kind="pdf", scope=Scope.NORMAL,
                state=RecordState.INDEXED, content_version="c1", indexed_generation=1, index_generation=1,
                chunk_count=5, text_chars=9000, profile_input_fingerprint=FP, keys_version=KEYS_VERSION,
                effective_date=datetime(2026, 5, 1, tzinfo=timezone.utc))
    base.update(kw)
    return DriveIndexRecord(**base)


PROFILE = DriveProfile(title="Lease", what_it_is="A flat lease", language="en", doc_type="contract")


# -- XC-3: describe_gap ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("profile_state,passages_state,gap", [
    (R, N, ep.GAP_WAITING), (D, N, ep.GAP_WAITING), (P, N, ep.GAP_WAITING), (C, R, ep.GAP_WAITING),
    (C, D, ep.GAP_WAITING), (C, P, ep.GAP_WAITING), (H, N, ep.GAP_HELD), (C, H, ep.GAP_HELD), (N, N, ""),
])
def test_a_file_without_a_description_counts_by_its_jobs(profile_state, passages_state, gap):
    assert ep.describe_gap(doc(profile_state=profile_state, passages_state=passages_state)) == gap


@pytest.mark.parametrize("profile_state,passages_state", [(D, D), (R, R), (P, D), (H, H), (D, C)])
def test_a_described_file_waiting_for_its_re_description_is_not_counted(profile_state, passages_state):
    rec = doc(profile=PROFILE, profile_state=profile_state, passages_state=passages_state, passages_done_at=NOW)
    assert ep.described(rec) and ep.describe_gap(rec) == ""


def test_a_profile_without_a_finished_passage_run_is_not_described_yet():
    rec = doc(profile=PROFILE, profile_state=C, passages_state=R)
    assert not ep.described(rec) and ep.describe_gap(rec) == ep.GAP_WAITING


def test_a_profile_is_enough_when_the_policy_gives_no_passages():
    rec = doc(kind="xlsx", name="budget.xlsx", profile=PROFILE, profile_state=C, passages_state=N)
    assert ep.described(rec) and ep.describe_gap(rec) == ep.GAP_NO_PASSAGES


def test_a_sheet_waiting_for_its_profile_is_not_described_and_not_no_passages():
    assert ep.describe_gap(doc(kind="xlsx", name="b.xlsx", profile_state=R, passages_state=N)) == ep.GAP_WAITING


def test_a_folder_is_never_counted():
    folder = doc(kind="folder", name="repo", is_folder=True, scope=Scope.OPAQUE_ROOT, profile_state=R)
    assert ep.describe_gap(folder) == ""


def test_planned_writes_describe_gap():
    rec = ep.planned(doc(), ctx())
    assert rec.profile_state == R and rec.describe_gap == ep.GAP_WAITING


def test_the_coverage_counters_name_every_gap():
    assert set(ep.COVERAGE_COUNTERS) == {ep.GAP_WAITING, ep.GAP_HELD, ep.GAP_NO_PASSAGES}
    assert all(name.startswith("coverage_") for name in ep.COVERAGE_COUNTERS.values())


# -- XC-6: cooldowns survive clears and template writes ------------------------------------------------------------

def _model_profiled(at: datetime) -> DriveIndexRecord:
    return ep.profiled(doc(), PROFILE, source=ProfileSource.MODEL, fingerprint=FP, now=at)


def test_a_template_write_keeps_the_model_profiles_cooldown():
    rec = _model_profiled(NOW - timedelta(hours=1))
    short = ep.profiled(replace(rec, text_chars=PROFILE_MIN_CHARS - 1), DriveProfile("", "note", "", "notes"),
                        source=ProfileSource.TEMPLATE, fingerprint="fp-short", now=NOW)
    assert short.profile_defer_until == NOW - timedelta(hours=1) + PROFILE_REFRESH_COOLDOWN


def test_a_note_crossing_the_template_length_back_is_not_profiled_again_within_the_cooldown():
    rec = _model_profiled(NOW - timedelta(hours=1))
    short = ep.profiled(replace(rec, text_chars=PROFILE_MIN_CHARS - 1), DriveProfile("", "note", "", "notes"),
                        source=ProfileSource.TEMPLATE, fingerprint="fp-short", now=NOW)
    long_again = replace(short, text_chars=9000, profile_input_fingerprint="fp-2")
    planned = ep.profile_plan(long_again, ctx())
    assert planned.profile_state == D and planned.profile_defer_until == short.profile_defer_until


def test_clearing_the_profile_keeps_its_cooldown():
    rec = _model_profiled(NOW - timedelta(hours=1))
    cleared = ep.profile_plan(replace(rec, indexed_generation=None), ctx())
    assert cleared.profile is None and cleared.profile_state == N
    assert cleared.profile_defer_until == NOW - timedelta(hours=1) + PROFILE_REFRESH_COOLDOWN
    back = ep.profile_plan(replace(cleared, indexed_generation=2, profile_input_fingerprint="fp-3"), ctx())
    assert back.profile_state == D


def test_after_the_cooldown_the_returned_text_is_profiled():
    rec = _model_profiled(NOW - PROFILE_REFRESH_COOLDOWN - timedelta(minutes=1))
    cleared = ep.profile_plan(replace(rec, indexed_generation=None), ctx())
    back = ep.profile_plan(replace(cleared, indexed_generation=2), ctx())
    assert back.profile_state == R


def test_clearing_the_passages_keeps_when_they_last_finished():
    done = ep.passages_advanced(ep.passages_started(doc()), 5, finished=True, now=NOW - timedelta(hours=2))
    cleared = ep.passages_plan(replace(done, indexed_generation=None), ctx())
    assert cleared.passages_state == N and cleared.passages_done_at == NOW - timedelta(hours=2)
    assert cleared.passages_generation is None and cleared.passages_next_index == 0
    again = ep.passages_plan(replace(cleared, indexed_generation=2), ctx())
    assert again.passages_state == D
    assert again.passages_defer_until == NOW - timedelta(hours=2) + PASSAGE_REFRESH_COOLDOWN


def test_a_cleared_passage_job_is_stable():
    done = ep.passages_advanced(ep.passages_started(doc()), 5, finished=True, now=NOW)
    cleared = ep.passages_plan(replace(done, indexed_generation=None), ctx())
    assert ep.passages_plan(cleared, ctx()) is cleared   # nothing left to clear: the same record back

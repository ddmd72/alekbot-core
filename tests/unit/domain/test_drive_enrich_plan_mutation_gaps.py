"""Boundaries of the enrichment plans that mutation testing showed unpinned (mutation_sv2.md, drive_enrich_plan): budget
clamps, the passage estimate, the effective-date fallback, the short note's fingerprint, the cooldown and due-time
boundaries, the December month turn, the settled state sets, the backoff streak and the cosine (USER_DRIVE_INDEXING_RFC
§25.4.5-§25.4.9)."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.domain import drive_enrich_plan as ep
from src.domain.drive_enrichment import (
    USD_CAPS, DriveProfile, EnrichContext, HoldReason, InitialBuild, JobState, ModelUsageMonth, ProfileDate,
    ProfileSource,
)
from src.domain.drive_index import (
    KEYS_VERSION, PASSAGES_VERSION, PROFILE_VERSION, DriveChunk, DriveIndexRecord, Priority, Reason, RecordState,
    Scope,
)
from src.domain.drive_index_params import (
    MODEL_BACKOFF_BASE_S, MODEL_BACKOFF_CAP_S, PASSAGE_CHUNK_USD_ESTIMATE, PASSAGE_REFRESH_COOLDOWN,
)

NOW = datetime(2026, 10, 20, 8, tzinfo=timezone.utc)
R, C, D, P, H, N = (JobState.READY.value, JobState.CURRENT.value, JobState.DEFERRED.value, JobState.PARKED.value,
                    JobState.HELD.value, JobState.NONE.value)


def ctx(**kw) -> EnrichContext:
    base = dict(now=NOW, month_usd={}, caps_usd=dict(USD_CAPS), initial_balance_usd=40.0, initial_open=True)
    base.update(kw)
    return EnrichContext(**base)


def doc(**kw) -> DriveIndexRecord:
    base = dict(user_id="u", item_id="d1", parent_id="root", name="lease.pdf", kind="pdf", scope=Scope.NORMAL,
                state=RecordState.INDEXED, content_version="c1", indexed_generation=1, index_generation=1,
                chunk_count=5, text_chars=9000, profile_input_fingerprint="fp", keys_version=KEYS_VERSION)
    base.update(kw)
    return DriveIndexRecord(**base)


# -- budget ----------------------------------------------------------------------------------------------------------

def test_an_overspent_initial_build_leaves_no_balance():
    context = ep.enrich_context(NOW, ModelUsageMonth(), InitialBuild(allowance_usd=40.0, spent_usd=41.0))
    assert context.initial_balance_usd == 0.0


def test_a_closed_build_adds_nothing_to_the_month():
    closed = ctx(initial_balance_usd=10.0, initial_open=False, month_usd={"profile": 1.0})
    assert ep.available_usd(closed, "profile") == pytest.approx(USD_CAPS["profile"] - 1.0)


def test_a_negative_balance_and_an_overspent_month_clamp_at_zero():
    assert ep.available_usd(ctx(initial_balance_usd=-5.0, month_usd={"profile": 999.0}), "profile") == 0.0
    assert ep.available_usd(ctx(initial_balance_usd=2.0, month_usd={"profile": 999.0}), "profile") == 2.0


@pytest.mark.parametrize("next_index,generation,chunks,total", [(2, 1, 2, 5), (4, 1, 1, 5), (0, 9, 2, 5),
                                                                (5, 1, 1, 5), (3, 9, 1, 1)])
def test_the_passage_estimate_is_the_next_calls_chunks(next_index, generation, chunks, total):
    rec = doc(passages_next_index=next_index, passages_generation=generation, chunk_count=total)
    assert ep.passages_estimate(rec) == pytest.approx(PASSAGE_CHUNK_USD_ESTIMATE * chunks)


# -- the effective date's fallback -----------------------------------------------------------------------------------

def test_with_no_date_anywhere_the_effective_date_is_now():
    assert ep.effective_from(doc(), None, now=NOW) == NOW


def test_with_no_date_and_no_now_there_is_none():
    assert ep.effective_from(doc(), None) is None


def test_the_records_own_date_wins_over_now():
    when = datetime(2025, 1, 2, tzinfo=timezone.utc)
    assert ep.effective_from(doc(effective_date=when), None, now=NOW) == when


# -- fingerprints and clears -----------------------------------------------------------------------------------------

def _chunk(text: str) -> DriveChunk:
    return DriveChunk(user_id="u", connection_id="u", item_id="d1", generation=1, index=0, text=text, char_start=0, char_end=len(text),
                      kind="markdown")


def test_a_short_notes_fingerprint_needs_text():
    note = doc(kind="markdown", name="n.md", text_chars=20)
    assert ep.input_fingerprint_of(note, [_chunk("   ")]) is None
    assert ep.input_fingerprint_of(note, [_chunk("buy milk")]) is not None


def test_clearing_a_documents_profile_clears_its_input_fingerprint_a_folders_keeps_it():
    cleared = ep.profile_plan(doc(indexed_generation=None, profile_state=R), ctx())
    assert cleared.profile_input_fingerprint is None
    folder = doc(kind="folder", name="repo", is_folder=True, scope=Scope.NORMAL, profile_state=R,
                 profile_input_fingerprint="fpf")
    assert ep.profile_plan(folder, ctx()).profile_input_fingerprint == "fpf"


def test_machine_text_without_a_profile_gets_its_template():
    rec = doc(kind="text", name="dump.txt", reason=Reason.MACHINE_TEXT, profile_source=ProfileSource.TEMPLATE.value,
              profile_version=PROFILE_VERSION, profile=None)
    planned = ep.profile_plan(rec, ctx())
    assert planned.profile is not None and planned.profile_state == C


def test_reverification_sets_and_clears_valid_until():
    profile = DriveProfile("t", "w", "en", "id_document",
                           dates=(ProfileDate("valid_until", datetime(2027, 3, 1).date()),))
    rec = ep.reverified(doc(), profile, NOW)
    assert rec.valid_until == datetime(2027, 3, 1, tzinfo=timezone.utc)
    assert ep.reverified(rec, DriveProfile("t", "w", "en", "id_document"), NOW).valid_until is None


def test_an_unused_passage_job_is_cleared_once_its_policy_gives_none():
    rec = doc(kind="code", name="a.py", passages_attempts=2, passages_state=N)
    assert ep.passages_plan(rec, ctx()).passages_attempts == 0


# -- time boundaries -------------------------------------------------------------------------------------------------

def test_the_passage_cooldown_ends_exactly_at_its_end():
    done = ep.passages_advanced(ep.passages_started(doc()), 5, finished=True, now=NOW - PASSAGE_REFRESH_COOLDOWN)
    again = ep.passages_plan(replace(done, indexed_generation=2), ctx())
    assert again.passages_state == R


def test_a_passage_run_resumes_on_its_generation_from_version_zero():
    rec = ep.passages_started(doc(passages_generation=1, passages_next_index=3, passages_version=0))
    assert (rec.passages_next_index, rec.passages_version) == (3, PASSAGES_VERSION)


def test_a_passage_run_restarts_on_another_version():
    rec = ep.passages_started(doc(passages_generation=1, passages_next_index=3, passages_version=PASSAGES_VERSION + 1))
    assert (rec.passages_next_index, rec.passages_version) == (0, PASSAGES_VERSION)


def test_keys_due_exactly_now_keep_their_priority():
    rec = doc(keys_version=KEYS_VERSION - 1, due_at=NOW, priority=int(Priority.RETRY))
    planned = ep.keys_planned(rec, NOW)
    assert planned.keys_pending and planned.priority == int(Priority.RETRY) and planned.due_at == NOW


def test_keys_due_later_are_pulled_to_now_at_maintenance():
    rec = doc(keys_version=KEYS_VERSION - 1, due_at=NOW + timedelta(days=9), priority=int(Priority.RETRY))
    planned = ep.keys_planned(rec, NOW)
    assert planned.due_at == NOW and planned.priority == int(Priority.MAINTENANCE)


def test_a_parked_job_in_december_wakes_on_the_first_of_january():
    december = datetime(2026, 12, 15, 9, tzinfo=timezone.utc)
    rec = ep.derived(doc(profile_state=P), december)
    assert rec.enrich_wake_at == datetime(2027, 1, 1, tzinfo=timezone.utc)


def test_a_parked_job_wakes_on_the_first_of_next_month():
    assert ep.derived(doc(passages_state=P), NOW).enrich_wake_at == datetime(2026, 11, 1, tzinfo=timezone.utc)


def test_a_deferred_profile_wakes_at_its_time_and_a_past_time_is_no_wake():
    later = NOW + timedelta(hours=3)
    assert ep.derived(doc(profile_state=D, profile_defer_until=later), NOW).enrich_wake_at == later
    assert ep.derived(doc(profile_state=D, profile_defer_until=NOW), NOW).enrich_wake_at is None


# -- settled per version key (the upgrade pass) ----------------------------------------------------------------------

def _held_profile(key_matches: bool) -> DriveIndexRecord:
    rec = doc()
    key = ep.profile_hold_key(rec) if key_matches else "0:old"
    return replace(rec, profile_hold=HoldReason.FAILED.value, profile_hold_version=key)


def test_the_profile_is_settled_when_held_for_the_current_key_only():
    assert ep.profile_settled(_held_profile(True), ctx())
    assert not ep.profile_settled(_held_profile(False), ctx())


def test_a_ready_profile_is_not_settled_a_kind_without_one_is():
    assert not ep.profile_settled(doc(), ctx())
    assert ep.profile_settled(doc(kind="code", name="a.py"), ctx())


def test_a_parked_profile_is_settled():
    spent = ctx(month_usd={"profile": 999.0}, initial_balance_usd=0.0, initial_open=False)
    assert ep.profile_settled(doc(), spent)


def test_the_passages_settled_states():
    assert not ep.passages_settled(doc(), ctx())
    assert ep.passages_settled(doc(kind="code", name="a.py"), ctx())
    done = ep.passages_advanced(ep.passages_started(doc()), 5, finished=True, now=NOW)
    assert ep.passages_settled(done, ctx())
    held = replace(doc(), passages_hold=HoldReason.FAILED.value, passages_hold_version=ep.passages_hold_key(doc()))
    assert ep.passages_settled(held, ctx())
    stale = replace(held, passages_hold_version="0:1")
    assert not ep.passages_settled(stale, ctx())


# -- the per-agent backoff streak ------------------------------------------------------------------------------------

def test_a_streak_continues_up_to_the_cap_after_the_last_backoff():
    first = ep.agent_outage({}, "a", NOW)
    assert first["a"] == NOW + timedelta(seconds=MODEL_BACKOFF_BASE_S)
    at_cap = first["a"] + timedelta(seconds=MODEL_BACKOFF_CAP_S)
    kept = ep.agent_outage(first, "a", at_cap)
    assert kept["a:since"] == NOW
    restarted = ep.agent_outage(first, "a", at_cap + timedelta(seconds=1))
    assert restarted["a:since"] == at_cap + timedelta(seconds=1)


def test_recovery_forgets_the_streak():
    assert ep.agent_recovered(ep.agent_outage({}, "a", NOW), "a") == {}


# -- cosine -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("a,b,expected", [([3, 4], [3, 4], 1.0), ([2, 0], [1, 0], 1.0), ([1, 0], [0, 1], 0.0),
                                          ([0, 0], [1, 1], 0.0), ([1, 0], [0, 0], 0.0), ([1, 1], [1, 0], 0.7071068)])
def test_similarity(a, b, expected):
    assert ep.similarity(a, b) == pytest.approx(expected)


def test_with_a_closed_build_a_class_has_exactly_its_cap_and_an_unknown_class_nothing():
    closed = ctx(initial_balance_usd=0.0, initial_open=False)
    assert ep.available_usd(closed, "profile") == pytest.approx(USD_CAPS["profile"])
    assert ep.available_usd(closed, "nope") == 0.0


def test_a_ready_profile_with_a_leftover_hold_key_is_not_settled():
    rec = doc()
    assert not ep.profile_settled(replace(rec, profile_hold_version=ep.profile_hold_key(rec)), ctx())

"""Search v2 P5 — the enrichment job plans (USER_DRIVE_INDEXING_RFC §25.4.5 state table, §25.4.7 edits, §25.4.9
clearing rules, §25.4.6 per-agent backoff). Pure: records in, records out."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from src.domain import drive_enrich_plan as ep
from src.domain.drive_enrichment import (
    DriveProfile, EnrichContext, InitialBuild, JobState, ModelUsageMonth, ProfileDate, ProfileEntity, ProfileSource,
    USD_CAPS,
)
from src.domain.drive_enrichment_calls import ModelReplyStatus
from src.domain.drive_index import (
    KEYS_VERSION, PASSAGES_VERSION, PROFILE_VERSION, DocumentBrief, DriveIndexRecord, Priority, Reason, RecordState,
    Scope, TriageVerdict,
)
from src.domain.drive_index_params import (
    MAX_ATTEMPTS, MODEL_BACKOFF_BASE_S, MODEL_BACKOFF_CAP_S, PASSAGE_MAX_CHUNKS_PER_FILE, PASSAGE_REFRESH_COOLDOWN,
    PROFILE_REFRESH_COOLDOWN, PROFILE_USD_PER_MONTH,
)

NOW = datetime(2026, 10, 20, 8, tzinfo=timezone.utc)
FP = "fp-1"


def ctx(**kw) -> EnrichContext:
    base = dict(now=NOW, month_usd={}, caps_usd=dict(USD_CAPS), initial_balance_usd=40.0, initial_open=True)
    base.update(kw)
    return EnrichContext(**base)


def spent_ctx(cls: str = "profile") -> EnrichContext:
    """The month's cap for `cls` and the initial build are spent."""
    return ctx(month_usd={cls: 999.0}, initial_balance_usd=0.0, initial_open=False)


def doc(**kw) -> DriveIndexRecord:
    base = dict(user_id="u", item_id="d1", parent_id="root", name="lease.pdf", kind="pdf", scope=Scope.NORMAL,
                state=RecordState.INDEXED, content_version="c1", indexed_generation=1, index_generation=1,
                chunk_count=5, text_chars=9000, profile_input_fingerprint=FP, keys_version=KEYS_VERSION,
                effective_date=datetime(2026, 5, 1, tzinfo=timezone.utc), fs_modified_at=datetime(2026, 5, 1,
                                                                                                tzinfo=timezone.utc))
    base.update(kw)
    return DriveIndexRecord(**base)


def profile(**kw) -> DriveProfile:
    base = dict(title="Lease", what_it_is="A flat lease", language="en", doc_type="contract")
    base.update(kw)
    return DriveProfile(**base)


def profiled_doc(**kw) -> DriveIndexRecord:
    rec = ep.profiled(doc(), profile(), source=ProfileSource.MODEL, fingerprint=FP, now=NOW - timedelta(days=3))
    return replace(rec, **kw)


# -- profile_plan: the state table -------------------------------------------------------------------------------

class TestProfilePlan:
    def test_a_readable_document_without_a_profile_is_ready(self):
        rec = ep.profile_plan(doc(), ctx())
        assert rec.profile_state == JobState.READY.value

    def test_a_kind_without_a_profile_policy_is_none(self):
        rec = ep.profile_plan(doc(kind="code", name="a.py"), ctx())
        assert rec.profile_state == JobState.NONE.value

    def test_nothing_readable_is_none_and_clears_the_profile(self):
        rec = ep.profile_plan(profiled_doc(indexed_generation=None, valid_until=NOW), ctx())
        assert rec.profile_state == JobState.NONE.value
        assert (rec.profile, rec.valid_until, rec.profile_source, rec.profile_version) == (None, None, None, 0)

    def test_a_matching_fingerprint_and_version_is_current(self):
        rec = ep.profile_plan(profiled_doc(), ctx())
        assert rec.profile_state == JobState.CURRENT.value

    def test_a_new_fingerprint_inside_the_cooldown_is_deferred(self):
        rec = ep.profiled(doc(), profile(), source=ProfileSource.MODEL, fingerprint=FP, now=NOW - timedelta(hours=2))
        rec = ep.profile_plan(replace(rec, profile_input_fingerprint="fp-2"), ctx())
        assert rec.profile_state == JobState.DEFERRED.value
        assert rec.profile_defer_until == NOW - timedelta(hours=2) + PROFILE_REFRESH_COOLDOWN

    def test_a_new_fingerprint_after_the_cooldown_is_ready(self):
        rec = ep.profile_plan(profiled_doc(profile_input_fingerprint="fp-2"), ctx())
        assert rec.profile_state == JobState.READY.value

    def test_a_profile_version_bump_is_ready_even_inside_the_cooldown(self):
        rec = ep.profiled(doc(), profile(), source=ProfileSource.MODEL, fingerprint=FP, now=NOW - timedelta(hours=2))
        rec = ep.profile_plan(replace(rec, profile_version=PROFILE_VERSION - 1), ctx())
        assert rec.profile_state == JobState.READY.value

    def test_a_hold_for_the_current_version_and_fingerprint_holds(self):
        rec = ep.profile_charged(doc(), ModelReplyStatus.REFUSED, NOW)
        assert (rec.profile_hold, rec.profile_hold_version) == ("refused", f"{PROFILE_VERSION}:{FP}")
        assert ep.profile_plan(rec, ctx()).profile_state == JobState.HELD.value

    def test_a_hold_under_another_fingerprint_is_not_a_match(self):
        rec = ep.profile_charged(doc(), ModelReplyStatus.REFUSED, NOW)
        rec = ep.profile_plan(replace(rec, profile_input_fingerprint="fp-2"), ctx())
        assert rec.profile_state == JobState.READY.value

    def test_spent_month_and_initial_build_park_the_job_until_the_month_turns(self):
        rec = ep.profile_plan(doc(), spent_ctx())
        assert (rec.profile_state, rec.profile_parked_month) == (JobState.PARKED.value, "2026-10")
        assert rec.profile_hold_version == f"{PROFILE_VERSION}:{FP}"

    def test_an_open_initial_build_pays_when_the_month_is_spent(self):
        rec = ep.profile_plan(doc(), ctx(month_usd={"profile": PROFILE_USD_PER_MONTH}))
        assert rec.profile_state == JobState.READY.value

    def test_a_short_note_gets_a_template_job_that_is_never_parked(self):
        note = doc(kind="markdown", name="todo.md", text_chars=40)
        assert ep.template_reason(note) is not None
        rec = ep.profile_plan(note, spent_ctx())
        assert rec.profile_state == JobState.READY.value

    def test_machine_text_gets_its_template_at_once(self):
        log = doc(kind="text", name="server.log.txt", state=RecordState.METADATA_ONLY, reason=Reason.MACHINE_TEXT,
                  indexed_generation=None, chunk_count=0)
        rec = ep.profile_plan(log, ctx())
        assert rec.profile_state == JobState.CURRENT.value and rec.profile_source == ProfileSource.TEMPLATE.value
        assert rec.profile.what_it_is == "machine-generated text"

    def test_a_script_rendered_page_gets_a_web_page_template(self):
        rec = ep.profile_plan(doc(kind="html", name="app.html", script_rendered=True), ctx())
        assert rec.profile_state == JobState.CURRENT.value and rec.profile.doc_type == "web_page"

    def test_a_collapsed_folder_with_a_readme_excerpt_is_ready(self):
        folder = DriveIndexRecord(user_id="u", connection_id="u", item_id="f1", name="repo", is_folder=True, kind="folder",
                                  scope=Scope.OPAQUE_ROOT, state=RecordState.METADATA_ONLY, reason=Reason.OPAQUE_ROOT,
                                  readme_excerpt="# Tool\n\nIt converts invoices.")
        rec = ep.profile_plan(folder, ctx())
        assert rec.profile_state == JobState.READY.value and rec.profile_input_fingerprint
        assert ep.profile_plan(replace(folder, readme_excerpt=""), ctx()).profile_state == JobState.NONE.value

    def test_a_scan_read_by_the_reader_is_profiled_as_a_document(self):
        scan = doc(kind="image", name="scan.jpg", triage_verdict=TriageVerdict.TEXT)
        assert ep.profile_plan(scan, ctx()).profile_state == JobState.READY.value

    def test_a_photograph_has_no_profile_job(self):
        photo = doc(kind="image", name="p.jpg", triage_verdict=TriageVerdict.PHOTO, state=RecordState.METADATA_ONLY,
                    reason=Reason.PHOTOGRAPH, indexed_generation=None)
        assert ep.profile_plan(photo, ctx()).profile_state == JobState.NONE.value


class TestProfileOutcomes:
    def test_a_profile_sets_valid_until_and_the_issue_date_as_effective_date(self):
        p = profile(dates=(ProfileDate("issued", date(2026, 3, 2)), ProfileDate("due", date(2026, 4, 1))))
        rec = ep.profiled(doc(), p, source=ProfileSource.MODEL, fingerprint=FP, now=NOW)
        assert rec.valid_until == datetime(2026, 4, 1, tzinfo=timezone.utc)
        assert rec.effective_date == datetime(2026, 3, 2, tzinfo=timezone.utc)
        assert (rec.profile_version, rec.profiled_content_version, rec.profile_fingerprint) == (PROFILE_VERSION, "c1",
                                                                                                FP)

    def test_the_profile_date_comes_before_the_brief_date(self):
        brief = DocumentBrief(doc_date=datetime(2025, 1, 1, tzinfo=timezone.utc))
        rec = ep.profiled(doc(brief=brief), profile(dates=(ProfileDate("issued", date(2026, 3, 2)),)),
                          source=ProfileSource.MODEL, fingerprint=FP, now=NOW)
        assert rec.effective_date == datetime(2026, 3, 2, tzinfo=timezone.utc)
        assert ep.effective_from(rec, None) == datetime(2025, 1, 1, tzinfo=timezone.utc)

    def test_clearing_the_profile_restores_the_file_date(self):
        rec = ep.profiled(doc(), profile(dates=(ProfileDate("issued", date(2026, 3, 2)),)),
                          source=ProfileSource.MODEL, fingerprint=FP, now=NOW)
        cleared = ep.profile_plan(replace(rec, indexed_generation=None), ctx())
        assert cleared.effective_date == datetime(2026, 5, 1, tzinfo=timezone.utc)

    @pytest.mark.parametrize("status", [ModelReplyStatus.FAILED, ModelReplyStatus.INVALID])
    def test_charged_failures_hold_failed_at_max_attempts(self, status):
        rec = doc()
        for _ in range(MAX_ATTEMPTS - 1):
            rec = ep.profile_charged(rec, status, NOW)
            assert rec.profile_hold is None
        rec = ep.profile_charged(rec, status, NOW)
        assert (rec.profile_hold, rec.profile_attempts) == ("failed", MAX_ATTEMPTS)
        assert ep.profile_plan(rec, ctx()).profile_state == JobState.HELD.value

    def test_stopped_at_cap_after_halving_holds_failed_at_once(self):
        rec = ep.profile_charged(doc(), ModelReplyStatus.STOPPED_AT_CAP, NOW)
        assert rec.profile_hold == "failed"

    def test_unavailable_charges_nothing(self):
        assert ep.profile_charged(doc(), ModelReplyStatus.UNAVAILABLE, NOW) == doc()


# -- passages_plan ------------------------------------------------------------------------------------------------

class TestPassagesPlan:
    def test_a_profiled_document_with_chunks_is_ready(self):
        assert ep.passages_plan(doc(), ctx()).passages_state == JobState.READY.value

    def test_kinds_without_doc2query_are_none(self):
        for kind, name in (("xlsx", "t.xlsx"), ("epub", "b.epub"), ("json", "d.json")):
            assert ep.passages_plan(doc(kind=kind, name=name), ctx()).passages_state == JobState.NONE.value

    def test_a_template_note_gets_no_passages(self):
        note = doc(kind="markdown", name="todo.md", text_chars=40)
        assert ep.passages_plan(note, ctx()).passages_state == JobState.NONE.value

    def test_done_for_this_generation_and_version_is_current(self):
        rec = ep.passages_advanced(ep.passages_started(doc()), 5, finished=True, now=NOW)
        assert ep.passages_plan(rec, ctx()).passages_state == JobState.CURRENT.value

    def test_chunks_to_hint_are_bounded_per_file(self):
        assert ep.chunks_to_hint(doc(chunk_count=PASSAGE_MAX_CHUNKS_PER_FILE + 40)) == PASSAGE_MAX_CHUNKS_PER_FILE

    def test_a_cut_run_resumes_without_a_cooldown(self):
        rec = ep.passages_advanced(ep.passages_started(doc()), 2, finished=False, now=NOW)
        rec = ep.passages_plan(rec, ctx())
        assert (rec.passages_state, rec.passages_next_index) == (JobState.READY.value, 2)

    def test_a_new_generation_inside_the_cooldown_waits(self):
        done = ep.passages_advanced(ep.passages_started(doc()), 5, finished=True, now=NOW - timedelta(hours=1))
        rec = ep.passages_plan(replace(done, indexed_generation=2), ctx())
        assert rec.passages_state == JobState.DEFERRED.value
        assert rec.passages_defer_until == NOW - timedelta(hours=1) + PASSAGE_REFRESH_COOLDOWN
        later = ep.passages_plan(replace(done, indexed_generation=2), ctx(now=NOW + PASSAGE_REFRESH_COOLDOWN))
        assert later.passages_state == JobState.READY.value

    def test_a_passages_version_bump_is_ready_at_once(self):
        done = ep.passages_advanced(ep.passages_started(doc()), 5, finished=True, now=NOW - timedelta(hours=1))
        rec = ep.passages_plan(replace(done, passages_version=PASSAGES_VERSION - 1), ctx())
        assert rec.passages_state == JobState.READY.value

    def test_held_failed_for_this_generation_only(self):
        rec = doc()
        for _ in range(MAX_ATTEMPTS):
            rec = ep.passages_charged(rec)
        assert (rec.passages_hold, rec.passages_hold_version) == ("failed", f"{PASSAGES_VERSION}:1")
        assert ep.passages_plan(rec, ctx()).passages_state == JobState.HELD.value
        assert ep.passages_plan(replace(rec, indexed_generation=2), ctx()).passages_state == JobState.READY.value

    def test_parked_when_the_passage_budget_is_spent(self):
        rec = ep.passages_plan(doc(), spent_ctx("passage"))
        assert (rec.passages_state, rec.passages_parked_month) == (JobState.PARKED.value, "2026-10")

    def test_dropping_the_readable_generation_clears_the_passage_fields(self):
        done = ep.passages_advanced(ep.passages_started(doc()), 5, finished=True, now=NOW)
        rec = ep.passages_plan(replace(done, indexed_generation=None), ctx())
        assert rec.passages_state == JobState.NONE.value
        assert (rec.passages_generation, rec.passages_next_index, rec.passages_done_at, rec.passages_version) == (
            None, 0, NOW, ep.PASSAGES_VERSION)
        # XC-6: when it finished and under which version are kept, so the refresh cooldown survives the clear


# -- keys, derived fields, the whole plan --------------------------------------------------------------------------

class TestPlanned:
    def test_keys_are_due_only_for_a_readable_generation_under_another_version(self):
        rec = ep.planned(doc(keys_version=KEYS_VERSION - 1), ctx())
        assert rec.keys_pending and (rec.due_at, rec.priority) == (NOW, int(Priority.MAINTENANCE))
        bare = ep.planned(doc(keys_version=0, indexed_generation=None, state=RecordState.PENDING), ctx())
        assert not bare.keys_pending and bare.keys_version == KEYS_VERSION

    def test_pending_keys_keep_an_earlier_schedule(self):
        # review rv_sv2_p5 MINOR 9 (spec change): keys work is due now even behind a later wait (a retry backoff stays
        # in `defer_until`, which content work honours); an already-due schedule and a more urgent class are kept.
        earlier = NOW - timedelta(hours=1)
        kept = ep.planned(doc(keys_version=0, due_at=earlier, priority=int(Priority.EDIT)), ctx())
        assert kept.keys_pending and (kept.due_at, kept.priority) == (earlier, int(Priority.EDIT))
        later = ep.planned(doc(keys_version=0, due_at=NOW + timedelta(hours=1), priority=int(Priority.RETRY)), ctx())
        assert later.keys_pending and (later.due_at, later.priority) == (NOW, int(Priority.MAINTENANCE))

    def test_derived_queue_fields(self):
        rec = ep.planned(doc(), ctx())
        assert rec.enrich_ready and rec.effective_day == "2026-05-01" and rec.enrich_wake_at is None
        deferred = ep.planned(replace(profiled_doc(profile_defer_until=NOW + timedelta(hours=3)),
                                      profile_input_fingerprint="fp-2",
                                      passages_state="current"), ctx())
        assert deferred.profile_state == JobState.DEFERRED.value
        assert deferred.enrich_wake_at == NOW + timedelta(hours=3) or deferred.enrich_ready

    def test_a_parked_job_wakes_when_the_month_turns(self):
        rec = ep.planned(doc(), spent_ctx("profile"))
        assert rec.profile_state == JobState.PARKED.value
        assert rec.enrich_wake_at == datetime(2026, 11, 1, tzinfo=timezone.utc)

    def test_about_keys_follow_the_profile(self):
        p = profile(entities=(ProfileEntity(name="Coimbra", type="place", printed="Coimbra"),))
        rec = ep.planned(ep.profiled(doc(), p, source=ProfileSource.MODEL, fingerprint=FP, now=NOW), ctx())
        assert "e:coimbra" in rec.about_keys and "l:type:contract" in rec.about_keys
        assert "e:coimbra" not in ep.planned(replace(rec, indexed_generation=None), ctx()).about_keys

    def test_a_planned_record_is_stable(self):
        once = ep.planned(doc(), ctx())
        assert ep.planned(once, ctx()) == once


class TestSettled:
    def test_ready_and_unplanned_jobs_are_not_settled(self):
        assert not ep.profile_settled(ep.planned(doc(), ctx()), ctx())
        assert not ep.profile_settled(doc(profile_state="current"), ctx())   # the stored state is stale

    def test_held_and_parked_for_the_current_version_are_settled(self):
        held = ep.planned(ep.profile_charged(doc(), ModelReplyStatus.REFUSED, NOW), ctx())
        assert ep.profile_settled(held, ctx())
        assert ep.profile_settled(doc(), spent_ctx("profile"))   # parked for the current version

    def test_deferred_is_not_settled(self):
        rec = ep.profiled(doc(), profile(), source=ProfileSource.MODEL, fingerprint=FP, now=NOW - timedelta(hours=2))
        assert not ep.profile_settled(replace(rec, profile_input_fingerprint="fp-2"), ctx())

    def test_passages_and_keys(self):
        done = ep.planned(ep.passages_advanced(ep.passages_started(profiled_doc()), 5, finished=True, now=NOW),
                          ctx())
        assert ep.passages_settled(done, ctx()) and ep.keys_settled(done)
        assert not ep.keys_settled(replace(done, keys_version=0))


# -- per-agent backoff (§25.4.6) ---------------------------------------------------------------------------------

class TestAgentBackoff:
    def test_an_outage_backs_off_one_agent_type(self):
        offs = ep.agent_outage({}, ep.PROFILER_AGENT, NOW)
        assert not ep.agent_available(offs, ep.PROFILER_AGENT, NOW)
        assert ep.agent_available(offs, ep.PASSAGES_AGENT, NOW)
        assert ep.agent_available(offs, ep.PROFILER_AGENT, NOW + timedelta(seconds=MODEL_BACKOFF_BASE_S))

    def test_a_streak_doubles_and_caps(self):
        offs = ep.agent_outage({}, ep.PROFILER_AGENT, NOW)
        t = NOW
        delays = []
        for _ in range(9):
            t = offs[ep.PROFILER_AGENT]
            offs = ep.agent_outage(offs, ep.PROFILER_AGENT, t)
            delays.append((offs[ep.PROFILER_AGENT] - t).total_seconds())
        assert delays[:3] == [MODEL_BACKOFF_BASE_S, 2 * MODEL_BACKOFF_BASE_S, 4 * MODEL_BACKOFF_BASE_S]
        assert max(delays) == MODEL_BACKOFF_CAP_S

    def test_recovery_ends_the_streak(self):
        offs = ep.agent_outage(ep.agent_outage({}, ep.PASSAGES_AGENT, NOW), ep.PROFILER_AGENT, NOW)
        offs = ep.agent_recovered(offs, ep.PROFILER_AGENT)
        assert ep.PROFILER_AGENT not in offs and ep.PASSAGES_AGENT in offs
        assert all(not k.startswith(ep.PROFILER_AGENT) for k in offs)


class TestBudget:
    def test_available_dollars_add_the_open_initial_build_to_the_months_rest(self):
        c = ep.enrich_context(NOW, ModelUsageMonth(profile_usd=1.0), InitialBuild(allowance_usd=40.0, spent_usd=30.0))
        assert ep.available_usd(c, "profile") == pytest.approx(10.0 + PROFILE_USD_PER_MONTH - 1.0)
        closed = ep.enrich_context(NOW, ModelUsageMonth(), InitialBuild(allowance_usd=40.0, spent_usd=40.0,
                                                                        closed_at=NOW))
        assert ep.available_usd(closed, "profile") == pytest.approx(PROFILE_USD_PER_MONTH)

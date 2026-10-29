"""Planner decisions for content (USER_DRIVE_INDEXING_RFC §5.1, §5.3, §7.3, §8, §12.3, §24). Pure. Admission is
switched on by rebinding the policy sets, as the flip will; with them empty, Slice 1 behaviour holds."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.domain import drive_index_policy as policy
from src.domain.drive_card import card_hash
from src.domain.drive_enrichment import DriveProfile, PhotoDescription
from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus, TriageResult
from src.domain.drive_index import (
    EXTRACTION_VERSION, TRIAGE_VERSION, DocumentBrief, DriveChange, DriveIndexRecord, DriveSyncState, EpochPhase,
    Priority, Reason, RecordState, Scope, TriageVerdict,
)
from src.domain.drive_index_params import (
    CARD_LEAD_CHARS, MAX_ATTEMPTS, MAX_DEFER, MAX_SHEET_DOWNLOAD_BYTES, MODEL_BACKOFF_BASE_S,
    MODEL_BACKOFF_CAP_S, QUIET, TICK_DEADLINE_S, TICK_START_BUDGET,
)
from src.domain.drive_index_planner import (
    PlanContext, attempt_crashed, backoff, can_start_file, classify, content_due, content_retry, demoted,
    generation_started, indexed, model_available, model_outage, model_recovered, month_key, plan_observation,
    schedule, stale_versions, triage_due, triaged, usage_now, valid_verdict, with_card, with_usage,
)
from src.domain.drive_index_policy import SLICE2_CONTENT_KINDS, SLICE2_TRIAGED_KINDS

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
FS = datetime(2026, 9, 1, 8, tzinfo=timezone.utc)
ROOT = "root"
SPACE = "test/8"


@pytest.fixture
def admitted(monkeypatch):
    monkeypatch.setattr(policy, "ADMITTED_KINDS", SLICE2_CONTENT_KINDS)
    monkeypatch.setattr(policy, "TRIAGED_KINDS", SLICE2_TRIAGED_KINDS)


def ctx(now=NOW):
    return PlanContext(user_id="u", root_item_id=ROOT, epoch=1, phase=EpochPhase.IDLE, now=now,
                       place_of=lambda lat, lon: None)


def ch(name="contract.pdf", item_id="a", cv="c1", size=1000, modified=NOW - timedelta(days=1), **kw):
    return DriveChange(item_id=item_id, name=name, parent_id=ROOT, size_bytes=size, content_version=cv,
                       modified_at=modified, fs_modified_at=FS, **kw)


def observe(change, existing=None, now=NOW, **kw):
    return plan_observation(change, existing, None, None, ctx(now), **kw).upsert


def carded(rec, now=NOW):
    """The tick embedded the card it was due for."""
    return with_card(rec, card_hash(rec), SPACE, now)


def text(body="Clause one. " * 300, **kw):
    return ExtractionOutcome(status=ExtractionStatus.TEXT, text=body, pages_total=3, extracted_by="parser@1", **kw)


def stored(change, now=NOW):
    """First sight, then the card."""
    return carded(observe(change, now=now), now)


def indexed_rec(change=None, gen=1, **kw):
    rec = stored(change or ch())
    return carded(indexed(generation_started(rec, NOW), outcome=text(**kw), generation=gen, chunk_count=4, now=NOW))


def image(cv="c1", **kw):
    return ch("IMG_1.jpg", item_id="img", cv=cv, mime_type="image/jpeg", **kw)


# -- a kind the flip leaves out stays as Slice 1 had it (doc: no extractor, §5.1, §24.1); audio and video wait for
# the transcript stage (policy 3, §27.3) with no content or triage work of the planner's ---------------------------

class TestNothingAdmitted:
    def test_a_document_stays_metadata_only(self):
        rec = observe(ch("contract.doc"))
        assert (rec.state, rec.reason) == (RecordState.METADATA_ONLY, Reason.KIND_NOT_ADMITTED)
        assert not content_due(rec) and not triage_due(rec)
        assert (rec.due_at, rec.priority) == (NOW, Priority.FIRST_SEEN)   # its card, as before

    @pytest.mark.parametrize("name,mime", [("clip.mp4", "video/mp4"), ("memo.m4a", "audio/mp4")])
    def test_a_video_waits_for_its_transcript(self, name, mime):   # policy 3 (§27.3): pending, never read here
        rec = observe(ch(name, item_id="vid", mime_type=mime))
        assert (rec.state, rec.reason) == (RecordState.PENDING, None)
        # no download, extraction or triage is ever planned for it: only its card is due, and once the card is
        # embedded nothing is left (the transcript stage, off by default, is the only reader of media)
        assert not content_due(rec) and not content_due(rec, NOW) and not triage_due(rec)
        assert (rec.due_at, rec.priority) == (NOW, Priority.FIRST_SEEN)
        done = carded(rec)
        assert (done.due_at, done.priority) == (None, None)
        assert not content_due(done) and not triage_due(done)

    def test_a_content_change_sets_no_quiet_period(self):
        rec = stored(ch("contract.doc"))
        edited = observe(ch("contract.doc", cv="c2", modified=NOW), rec)
        assert edited.dirty_since is None and edited.due_at is None


# -- classify ------------------------------------------------------------------------------------------------

@pytest.mark.usefixtures("admitted")
class TestClassifyContent:
    def test_admitted_kind_is_pending(self):
        cls = classify(ch(), Scope.NORMAL, None, None)
        assert (cls.state, cls.reason, cls.content_kept) == (RecordState.PENDING, None, False)

    def test_kind_without_an_extractor_is_not_admitted(self):
        assert classify(ch("old.doc"), Scope.NORMAL, None, None).reason is Reason.KIND_NOT_ADMITTED

    def test_item_rules_come_first(self):
        assert classify(ch(".hidden.pdf"), Scope.NORMAL, None, None).reason is Reason.DOT_NAME
        assert classify(ch("~$contract.docx"), Scope.NORMAL, None, None).reason is Reason.OS_ARTIFACT

    def test_over_the_download_cap(self):
        cls = classify(ch(size=policy.size_cap("pdf") + 1), Scope.NORMAL, None, None)
        assert (cls.state, cls.reason) == (RecordState.METADATA_ONLY, Reason.TOO_LARGE)

    def test_at_the_download_cap(self):
        assert classify(ch(size=policy.size_cap("pdf")), Scope.NORMAL, None, None).state is RecordState.PENDING

    def test_sheets_have_their_own_cap(self):
        assert classify(ch("t.xlsx", size=MAX_SHEET_DOWNLOAD_BYTES + 1), Scope.NORMAL, None,
                        None).reason is Reason.TOO_LARGE
        assert classify(ch("t.pdf", size=MAX_SHEET_DOWNLOAD_BYTES + 1), Scope.NORMAL, None,
                        None).state is RecordState.PENDING

    def test_indexed_outcome_stands_at_the_same_version(self):
        cls = classify(ch("renamed.pdf"), Scope.NORMAL, None, indexed_rec())
        assert (cls.state, cls.content_kept) == (RecordState.INDEXED, True)

    def test_indexed_outcome_ends_with_new_content(self):
        assert classify(ch(cv="c2"), Scope.NORMAL, None, indexed_rec()).state is RecordState.PENDING

    def test_indexed_outcome_ends_with_new_extraction_rules(self):
        old = replace(indexed_rec(), extraction_version=EXTRACTION_VERSION + 1)
        assert classify(ch(), Scope.NORMAL, None, old).state is RecordState.PENDING

    @pytest.mark.parametrize("reason", [Reason.NO_TEXT, Reason.MACHINE_TEXT, Reason.ENCRYPTED, Reason.TOO_LARGE,
                                        Reason.USER_CHUNK_CAP, Reason.KIND_NOT_ADMITTED])
    def test_content_demotion_stands(self, reason):
        prev = demoted(stored(ch()), reason, NOW)
        cls = classify(ch("renamed.pdf"), Scope.NORMAL, None, prev)
        assert (cls.state, cls.reason, cls.content_kept) == (RecordState.METADATA_ONLY, reason, True)

    def test_metadata_reason_without_an_extraction_never_stands(self):
        slice1 = replace(stored(ch()), state=RecordState.METADATA_ONLY, reason=Reason.KIND_NOT_ADMITTED)
        assert slice1.extraction_version == 0
        assert classify(ch(), Scope.NORMAL, None, slice1).state is RecordState.PENDING

    def test_failed_stands_until_the_content_changes(self):
        failed = content_retry(replace(stored(ch()), attempts=MAX_ATTEMPTS - 1), Reason.EXTRACT_ERROR, NOW)
        assert classify(ch(), Scope.NORMAL, None, failed).state is RecordState.FAILED
        assert classify(ch(cv="c2"), Scope.NORMAL, None, failed).state is RecordState.PENDING

    def test_oversized_new_version_beats_a_standing_outcome(self):
        cls = classify(ch(cv="c1", size=policy.size_cap("pdf") + 1), Scope.NORMAL, None, indexed_rec())
        assert (cls.reason, cls.content_kept) == (Reason.TOO_LARGE, False)


@pytest.mark.usefixtures("admitted")
class TestClassifyTriage:
    def verdict(self, v, cv="c1", version=TRIAGE_VERSION):
        return replace(stored(image()), triage_verdict=v, triage_version=version, triaged_content_version=cv)

    def test_no_verdict_is_pending(self):
        assert classify(image(), Scope.NORMAL, None, None).state is RecordState.PENDING

    def test_photo_verdict(self):
        cls = classify(image(), Scope.NORMAL, None, self.verdict(TriageVerdict.PHOTO))
        assert (cls.state, cls.reason) == (RecordState.METADATA_ONLY, Reason.PHOTOGRAPH)

    def test_text_verdict(self):
        assert classify(image(), Scope.NORMAL, None, self.verdict(TriageVerdict.TEXT)).state is RecordState.PENDING

    def test_verdict_on_another_content_version_is_void(self):
        cls = classify(image(cv="c2"), Scope.NORMAL, None, self.verdict(TriageVerdict.PHOTO))
        assert (cls.state, cls.reason) == (RecordState.PENDING, None)

    def test_verdict_under_an_older_triage_version_is_void(self):
        prev = self.verdict(TriageVerdict.PHOTO, version=TRIAGE_VERSION - 1)
        assert classify(image(), Scope.NORMAL, None, prev).state is RecordState.PENDING
        assert valid_verdict(prev, "c1") is None

    def test_read_image_outcome_stands(self):
        prev = triaged(stored(image()), TriageResult("img", TriageVerdict.TEXT), NOW)
        prev = indexed(generation_started(prev, NOW), outcome=text(), generation=1, chunk_count=1, now=NOW)
        assert classify(image(), Scope.NORMAL, None, prev).state is RecordState.INDEXED


# -- what is due --------------------------------------------------------------------------------------------

@pytest.mark.usefixtures("admitted")
class TestDue:
    def test_first_sight_is_due_at_once_in_class_2(self):
        rec = observe(ch(modified=NOW))
        assert content_due(rec) and (rec.due_at, rec.priority) == (NOW, Priority.FIRST_SEEN)

    def test_indexed_is_not_due(self):
        rec = indexed_rec()
        assert not content_due(rec) and rec.due_at is None

    def test_pending_with_a_current_generation_is_not_due(self):
        rec = replace(indexed_rec(), state=RecordState.PENDING)
        assert not content_due(rec)

    def test_pending_with_an_older_generation_is_due(self):
        rec = replace(indexed_rec(), state=RecordState.PENDING, content_version="c2")
        assert content_due(rec)

    def test_a_started_run_is_still_due(self):
        assert content_due(generation_started(observe(ch()), NOW))

    def test_retried_run_is_still_due(self):
        assert content_due(content_retry(observe(ch()), Reason.EXTRACT_TIMEOUT, NOW))

    def test_not_admitted_is_never_content_due(self, monkeypatch):
        rec = observe(ch())
        monkeypatch.setattr(policy, "ADMITTED_KINDS", frozenset())
        assert not content_due(rec)

    def test_image_without_a_verdict_is_triage_due_only(self):
        rec = observe(image())
        assert triage_due(rec) and not content_due(rec)
        assert (rec.due_at, rec.priority) == (NOW, Priority.FIRST_SEEN)

    def test_text_image_is_content_due_only(self):
        rec = triaged(observe(image()), TriageResult("img", TriageVerdict.TEXT), NOW)
        assert content_due(rec) and not triage_due(rec)

    def test_photo_is_neither(self):
        rec = triaged(observe(image()), TriageResult("img", TriageVerdict.PHOTO, "a beach"), NOW)
        assert not content_due(rec) and not triage_due(rec)


# -- the quiet period (§7.3) ---------------------------------------------------------------------------------

@pytest.mark.usefixtures("admitted")
class TestQuietPeriod:
    def test_an_edit_waits_for_quiet(self):
        rec = observe(ch(cv="c2", modified=NOW), indexed_rec())
        assert rec.state is RecordState.PENDING and rec.dirty_since == NOW
        assert (rec.due_at, rec.priority) == (NOW + QUIET, Priority.EDIT)

    def test_an_old_modification_is_due_at_once(self):
        rec = observe(ch(cv="c2", modified=NOW - timedelta(hours=1)), indexed_rec())
        assert rec.due_at == NOW - timedelta(hours=1) + QUIET

    def test_autosaves_move_the_due_time_until_max_defer(self):
        rec = observe(ch(cv="c2", modified=NOW), indexed_rec())
        t = NOW
        for n in range(3, 20):
            t = NOW + timedelta(minutes=3 * n)
            rec = observe(ch(cv=f"c{n}", modified=t), rec, now=t)
            assert rec.dirty_since == NOW
            assert rec.due_at == min(t + QUIET, NOW + MAX_DEFER)
        assert rec.due_at == NOW + MAX_DEFER

    def test_an_edit_restarts_the_attempts(self):
        retrying = content_retry(observe(ch(cv="c2", modified=NOW), indexed_rec()), Reason.EXTRACT_ERROR, NOW)
        assert retrying.attempts == 1
        assert observe(ch(cv="c3", modified=NOW), retrying).attempts == 0

    def test_a_nudge_is_due_at_once(self):
        rec = observe(ch(cv="c2", modified=NOW), indexed_rec(), nudged=True)
        assert rec.dirty_since is None and rec.due_at == NOW and content_due(rec)

    def test_a_nudge_after_an_edit_ends_its_quiet_period(self):
        dirty = observe(ch(cv="c2", modified=NOW), indexed_rec())
        rec = observe(ch(cv="c2", modified=NOW), dirty, nudged=True)
        assert rec is not None and rec.dirty_since is None and rec.due_at == NOW

    def test_the_same_change_again_keeps_the_record(self):
        dirty = observe(ch(cv="c2", modified=NOW), indexed_rec())
        assert plan_observation(ch(cv="c2", modified=NOW), dirty, None, None, ctx()).upsert is None

    def test_quiet_period_survives_a_card_write(self):
        dirty = observe(ch(cv="c2", modified=NOW), indexed_rec())
        assert carded(dirty).due_at == NOW + QUIET

    def test_dirty_clears_on_a_terminal_state(self):
        dirty = observe(ch(cv="c2", modified=NOW), indexed_rec())
        done = indexed(generation_started(dirty, NOW), outcome=text(), generation=2, chunk_count=4, now=NOW)
        assert done.dirty_since is None
        assert demoted(dirty, Reason.NO_TEXT, NOW).dirty_since is None

    def test_dirty_clears_when_a_rename_makes_it_metadata_only(self):
        dirty = observe(ch(cv="c2", modified=NOW), indexed_rec())
        assert observe(ch(".c.pdf", cv="c2", modified=NOW), dirty).dirty_since is None

    def test_rename_of_an_indexed_document_costs_a_card_only(self):
        rec = observe(ch("renamed.pdf"), indexed_rec())
        assert rec.state is RecordState.INDEXED and not content_due(rec)
        assert (rec.due_at, rec.priority) == (NOW, Priority.EDIT)


@pytest.mark.usefixtures("admitted")
class TestSchedule:
    def test_earliest_time_and_most_urgent_class(self):
        dirty = observe(ch(cv="c2", modified=NOW), indexed_rec())
        renamed = observe(ch("renamed.pdf", cv="c2", modified=NOW), dirty)   # card now, content after QUIET
        assert (renamed.due_at, renamed.priority) == (NOW, Priority.EDIT)
        assert carded(renamed).due_at == NOW + QUIET

    def test_first_seen_content_and_card_share_class_2(self):
        rec = schedule(observe(ch()), NOW, first_seen=True)
        assert (rec.due_at, rec.priority) == (NOW, Priority.FIRST_SEEN)


# -- transitions (§7.4, §8.1, §8.2) ----------------------------------------------------------------------------

@pytest.mark.usefixtures("admitted")
class TestRuns:
    def test_generation_started(self):
        rec = stored(ch())
        run = generation_started(rec, NOW)
        assert run.index_generation == rec.index_generation + 1 and run.attempt_started_at == NOW
        assert (run.due_at, run.priority) == (NOW + timedelta(seconds=TICK_DEADLINE_S), Priority.RETRY)
        assert (run.defer_until, run.defer_priority) == (run.due_at, Priority.RETRY)

    def test_generation_started_leaves_a_due_card_due(self):
        run = generation_started(observe(ch()), NOW)
        assert (run.due_at, run.priority) == (NOW, Priority.FIRST_SEEN) and not content_due(run, NOW)
        assert generation_started(run, NOW).index_generation == run.index_generation + 1   # never reused

    def test_attempt_crashed(self):
        run = generation_started(observe(ch()), NOW)
        assert not attempt_crashed(observe(ch()), NOW)
        assert not attempt_crashed(run, NOW + timedelta(seconds=TICK_DEADLINE_S - 1))
        assert attempt_crashed(run, NOW + timedelta(seconds=TICK_DEADLINE_S))

    def test_indexed(self):
        run = generation_started(observe(ch()), NOW)
        body = "  " + "x" * (CARD_LEAD_CHARS + 50)
        rec = indexed(run, outcome=text(body, truncated=True), generation=run.index_generation, chunk_count=7,
                      now=NOW)
        assert (rec.state, rec.reason) == (RecordState.INDEXED, None)
        assert (rec.indexed_generation, rec.indexed_version, rec.extraction_version) == (1, "c1", EXTRACTION_VERSION)
        assert (rec.extracted_by, rec.text_chars, rec.pages_total, rec.chunk_count, rec.truncated) == \
            ("parser@1", len(body), 3, 7, True)
        assert rec.lead_text == ""   # v2 (§25.2.7): no lead text but for machine text; the profile carries the card
        assert (rec.attempts, rec.attempt_started_at, rec.dirty_since) == (0, None, None)
        assert (rec.due_at, rec.priority) == (NOW, Priority.FIRST_SEEN)   # never carded: its first card is due
        assert not content_due(rec)

    def test_a_carded_record_refreshes_its_card_in_class_0(self):
        # v2: the read text no longer reaches the card by itself (no lead text); a brief does, so the card changes
        brief = DocumentBrief(description="an invoice")   # Δ64: a brief's doc type is no longer a card segment
        rec = indexed(generation_started(stored(ch()), NOW), outcome=text(brief=brief), generation=1, chunk_count=1,
                      now=NOW)
        assert (rec.due_at, rec.priority) == (NOW, Priority.EDIT)

    def test_a_carded_record_whose_card_is_unchanged_is_not_rescheduled(self):
        rec = indexed(generation_started(stored(ch()), NOW), outcome=text(), generation=1, chunk_count=1, now=NOW)
        assert (rec.due_at, rec.priority) == (None, None)

    def test_indexed_brief_sets_the_effective_date(self):
        brief = DocumentBrief(doc_type="invoice", doc_date=datetime(2024, 3, 15, tzinfo=timezone.utc))
        rec = indexed(observe(ch()), outcome=text(brief=brief), generation=1, chunk_count=1, now=NOW)
        assert rec.brief == brief and rec.effective_date == datetime(2024, 3, 15, tzinfo=timezone.utc)

    def test_indexed_without_a_brief_keeps_the_file_date(self):
        rec = indexed(observe(ch()), outcome=text(), generation=1, chunk_count=1, now=NOW)
        assert rec.brief is None and rec.effective_date == FS

    def test_reindex_without_a_brief_returns_to_the_file_date(self):
        brief = DocumentBrief(doc_date=datetime(2024, 3, 15, tzinfo=timezone.utc))
        first = indexed(observe(ch()), outcome=text(brief=brief), generation=1, chunk_count=1, now=NOW)
        again = indexed(replace(first, content_version="c2"), outcome=text(), generation=2, chunk_count=1, now=NOW)
        assert again.effective_date == FS

    @pytest.mark.parametrize("reason", [Reason.NO_TEXT, Reason.ENCRYPTED, Reason.TOO_LARGE, Reason.KIND_NOT_ADMITTED])
    def test_demoted(self, reason):
        rec = demoted(indexed_rec(), reason, NOW, outcome=text("some text"))
        assert (rec.state, rec.reason) == (RecordState.METADATA_ONLY, reason)
        assert rec.indexed_generation is None and rec.lead_text == "" and rec.chunk_count == 0
        assert (rec.extraction_version, rec.attempts, rec.dirty_since) == (EXTRACTION_VERSION, 0, None)
        assert not content_due(rec)

    def test_machine_text_keeps_its_lead_text(self):
        rec = demoted(observe(ch()), Reason.MACHINE_TEXT, NOW, outcome=text("2026-10-01 GET /x 200\n" * 200))
        assert rec.lead_text.startswith("2026-10-01 GET /x 200") and len(rec.lead_text) <= CARD_LEAD_CHARS
        assert rec.text_chars > 0

    def test_chunk_cap_keeps_the_would_be_chunk_count(self):
        rec = demoted(observe(ch()), Reason.USER_CHUNK_CAP, NOW, outcome=text(), chunk_count=120)
        assert rec.chunk_count == 120 and rec.indexed_generation is None

    def test_demotion_keeps_a_brief(self):
        brief = DocumentBrief(doc_type="receipt", doc_date=datetime(2024, 1, 2, tzinfo=timezone.utc))
        # the read text prints the brief's date (Δ60: a demotion grades it before it dates the file)
        rec = demoted(observe(ch()), Reason.USER_CHUNK_CAP, NOW,
                      outcome=text(body="Receipt of 02.01.2024. " + "Clause one. " * 300, brief=brief), chunk_count=9)
        assert rec.brief == replace(brief, date_grade="in_text") and rec.effective_date == brief.doc_date

    def test_demoted_refuses_a_structural_reason(self):
        with pytest.raises(ValueError):
            demoted(observe(ch()), Reason.DOT_NAME, NOW)

    def test_content_retry_backs_off(self):
        run = generation_started(stored(ch()), NOW)
        rec = content_retry(run, Reason.EXTRACT_TIMEOUT, NOW)
        assert (rec.attempts, rec.attempt_started_at, rec.state) == (1, None, RecordState.PENDING)
        assert (rec.due_at, rec.priority) == (NOW + backoff(1), Priority.RETRY)
        assert rec.defer_until == NOW + backoff(1)

    def test_content_retry_fails_at_max_attempts_keeping_old_chunks(self):
        old = replace(indexed_rec(), state=RecordState.PENDING, content_version="c2", attempts=MAX_ATTEMPTS - 1)
        rec = content_retry(old, Reason.EXTRACT_MEMORY, NOW)
        assert (rec.state, rec.reason) == (RecordState.FAILED, Reason.EXTRACT_MEMORY)
        assert rec.indexed_generation == 1 and rec.stale
        assert (rec.attempts, rec.attempt_started_at, rec.dirty_since) == (0, None, None)
        assert not content_due(rec)

    def test_content_retry_refuses_a_deterministic_reason(self):
        with pytest.raises(ValueError):
            content_retry(observe(ch()), Reason.NO_TEXT, NOW)


@pytest.mark.usefixtures("admitted")
class TestTriaged:
    def test_photo(self):
        rec = triaged(stored(image()), TriageResult("img", TriageVerdict.PHOTO), NOW,
                      photo=PhotoDescription(description="a cathedral at night", scene="old town"))
        assert (rec.state, rec.reason, rec.photo_description, rec.photo_scene) == (
            RecordState.METADATA_ONLY, Reason.PHOTOGRAPH, "a cathedral at night", "old town")
        assert (rec.triage_verdict, rec.triage_version, rec.triaged_content_version) == \
            (TriageVerdict.PHOTO, TRIAGE_VERSION, "c1")
        assert rec.due_at == NOW   # the description changed the card

    def test_photo_drops_what_a_reading_left(self):
        read = triaged(stored(image()), TriageResult("img", TriageVerdict.TEXT), NOW)
        read = indexed(read, outcome=text(brief=DocumentBrief(doc_type="receipt")), generation=1, chunk_count=1,
                       now=NOW)
        rec = triaged(replace(read, triage_version=0), TriageResult("img", TriageVerdict.PHOTO, "a dog"), NOW)
        assert (rec.indexed_generation, rec.lead_text, rec.brief) == (None, "", None)

    def test_text_makes_the_reader_due(self):
        rec = triaged(stored(image()), TriageResult("img", TriageVerdict.TEXT, "ignored"), NOW)
        assert (rec.state, rec.caption) == (RecordState.PENDING, "") and content_due(rec)

    def test_text_on_an_indexed_version_restores_indexed(self):
        read = triaged(stored(image()), TriageResult("img", TriageVerdict.TEXT), NOW)
        read = indexed(read, outcome=text(), generation=1, chunk_count=1, now=NOW)
        rec = triaged(replace(read, state=RecordState.PENDING, triage_version=0),
                      TriageResult("img", TriageVerdict.TEXT), NOW)
        assert rec.state is RecordState.INDEXED and not content_due(rec)

    @pytest.mark.parametrize("before,after", [(Priority.EDIT, Priority.EDIT),
                                              (Priority.FIRST_SEEN, Priority.FIRST_SEEN),
                                              (None, Priority.FIRST_SEEN)])
    def test_the_reader_keeps_the_class_of_the_triage(self, before, after):
        rec = replace(carded(observe(image())), priority=before)
        assert triaged(rec, TriageResult("img", TriageVerdict.TEXT), NOW).priority == after

    def test_mismatched_item(self):
        with pytest.raises(ValueError):
            triaged(observe(image()), TriageResult("other", TriageVerdict.PHOTO), NOW)

    def test_a_photo_keeps_its_verdict_and_description_on_rename(self):
        photo = carded(triaged(stored(image()), TriageResult("img", TriageVerdict.PHOTO), NOW,
                               photo=PhotoDescription(description="a beach")))
        rec = observe(replace(image(), name="beach.jpg"), photo)
        assert (rec.reason, rec.photo_description) == (Reason.PHOTOGRAPH, "a beach") and not triage_due(rec)

    def test_new_content_needs_a_new_verdict(self):
        photo = carded(triaged(stored(image()), TriageResult("img", TriageVerdict.PHOTO, "a beach"), NOW))
        rec = observe(image(cv="c2", modified=NOW), photo)
        assert rec.state is RecordState.PENDING and triage_due(rec)

    def test_rollback_of_triage_drops_the_description(self, monkeypatch):
        # v2 (§25.4.9): the kind leaving triage clears the photo fields (the v1 caption's successor)
        photo = carded(triaged(stored(image()), TriageResult("img", TriageVerdict.PHOTO), NOW,
                               photo=PhotoDescription(description="a beach", scene="coast", visible_text="BAR")))
        assert photo.photo_description == "a beach"
        monkeypatch.setattr(policy, "TRIAGED_KINDS", frozenset())
        rec = observe(image(), photo)
        assert (rec.reason, rec.photo_description, rec.photo_scene, rec.visible_text) == (
            Reason.KIND_NOT_ADMITTED, "", "", "")


@pytest.mark.usefixtures("admitted")
class TestObservationKeepsContent:
    def test_brief_date_holds_on_re_observation(self):
        brief = DocumentBrief(doc_date=datetime(2024, 3, 15, tzinfo=timezone.utc))
        rec = carded(indexed(stored(ch()), outcome=text(brief=brief), generation=1, chunk_count=1, now=NOW))
        assert observe(ch("renamed.pdf"), rec).effective_date == brief.doc_date

    def test_a_structural_demotion_clears_the_content(self):
        brief = DocumentBrief(doc_date=datetime(2024, 3, 15, tzinfo=timezone.utc))
        rec = carded(indexed(stored(ch()), outcome=text(brief=brief), generation=1, chunk_count=1, now=NOW))
        hidden = observe(ch(".contract.pdf"), rec)
        assert (hidden.reason, hidden.indexed_generation, hidden.lead_text, hidden.brief) == \
            (Reason.DOT_NAME, None, "", None)
        assert hidden.effective_date == FS

    def test_a_content_change_keeps_the_old_chunks_readable(self):
        # v2 (§25.4.9): what the content gave the card (the profile, no longer lead text) is kept until the new read
        profile = DriveProfile(title="Lease", what_it_is="A flat lease", language="en", doc_type="contract")
        rec = observe(ch(cv="c2", modified=NOW), replace(indexed_rec(), profile=profile))
        assert rec.indexed_generation == 1 and rec.stale and rec.profile == profile


# -- versions (§12.3) ----------------------------------------------------------------------------------------

class TestStaleVersions:
    def base(self, **kw):
        return replace(observe(ch()), **kw)

    def test_slice1_record_is_not_stale(self):
        assert not stale_versions(self.base())

    def test_extracted_under_the_current_rules(self):
        assert not stale_versions(self.base(extraction_version=EXTRACTION_VERSION))

    def test_extracted_under_other_rules(self):
        assert stale_versions(self.base(extraction_version=EXTRACTION_VERSION + 1))

    def test_triaged_under_an_older_version(self):
        assert stale_versions(self.base(triage_verdict=TriageVerdict.PHOTO, triage_version=TRIAGE_VERSION - 1))
        assert not stale_versions(self.base(triage_verdict=TriageVerdict.PHOTO, triage_version=TRIAGE_VERSION))


# -- monthly usage, model outages, the file start rule (§24.3, §24.7, §24.8) -----------------------------------

class TestUsage:
    def test_month_key_is_utc(self):
        assert month_key(NOW) == "2026-10"
        assert month_key(datetime(2026, 11, 1, 0, 30, tzinfo=timezone(timedelta(hours=2)))) == "2026-10"

    def test_counts_within_a_month(self):
        state = with_usage(with_usage(DriveSyncState(user_id="u"), NOW, pages=10), NOW, pages=5, images=16)
        assert state.usage_month == "2026-10" and usage_now(state, NOW) == (15, 16)

    def test_a_new_month_starts_at_zero(self):
        end = datetime(2026, 10, 31, 23, 59, tzinfo=timezone.utc)
        state = with_usage(DriveSyncState(user_id="u"), end, pages=4_999, images=100)
        later = end + timedelta(minutes=2)
        assert usage_now(state, end) == (4_999, 100) and usage_now(state, later) == (0, 0)
        assert usage_now(with_usage(state, later, pages=3), later) == (3, 0)
        assert with_usage(state, later, pages=3).usage_month == "2026-11"

    def test_a_fresh_state_has_no_usage(self):
        assert usage_now(DriveSyncState(user_id="u"), NOW) == (0, 0)


class TestModelOutage:
    def test_available_by_default(self):
        assert model_available(DriveSyncState(user_id="u"), NOW)

    def test_backoff_doubles_to_its_cap(self):
        state = DriveSyncState(user_id="u")
        delays = []
        for _ in range(12):
            state = model_outage(state, NOW)
            delays.append((state.model_backoff_until - NOW).total_seconds())
        assert delays[:3] == [MODEL_BACKOFF_BASE_S, 2 * MODEL_BACKOFF_BASE_S, 4 * MODEL_BACKOFF_BASE_S]
        assert delays[-1] == MODEL_BACKOFF_CAP_S and state.model_failures == 12

    def test_unavailable_until_the_backoff_ends(self):
        state = model_outage(DriveSyncState(user_id="u"), NOW)
        assert not model_available(state, NOW + timedelta(seconds=MODEL_BACKOFF_BASE_S - 1))
        assert model_available(state, NOW + timedelta(seconds=MODEL_BACKOFF_BASE_S))

    def test_recovery_resets(self):
        state = model_recovered(model_outage(model_outage(DriveSyncState(user_id="u"), NOW), NOW))
        assert (state.model_failures, state.model_backoff_until) == (0, None) and model_available(state, NOW)


class TestCanStartFile:
    def test_a_fresh_tick_can_start_a_routed_file(self):
        assert can_start_file(0.0, routed=True)

    def test_a_routed_file_fits_at_the_end_of_the_start_budget(self):
        """§24.3: the configured per-file budgets fit the tick deadline after the whole start budget."""
        assert can_start_file(TICK_START_BUDGET.total_seconds(), routed=True)

    def test_the_bound_is_strict(self):
        from src.domain.drive_index_params import (
            DOWNLOAD_TIMEOUT_S, EMBED_FILE_BUDGET_S, EXTRACT_WALL_TIMEOUT_S, FILE_START_MARGIN_S, MODEL_FILE_BUDGET_S,
        )
        parsed = DOWNLOAD_TIMEOUT_S + EXTRACT_WALL_TIMEOUT_S + EMBED_FILE_BUDGET_S + FILE_START_MARGIN_S
        assert not can_start_file(TICK_DEADLINE_S - parsed, routed=False)
        assert can_start_file(TICK_DEADLINE_S - parsed - 0.5, routed=False)
        assert not can_start_file(TICK_DEADLINE_S - parsed - MODEL_FILE_BUDGET_S, routed=True)

    def test_a_parsed_file_can_start_later_than_a_routed_one(self):
        from src.domain.drive_index_params import (
            DOWNLOAD_TIMEOUT_S, EMBED_FILE_BUDGET_S, EXTRACT_WALL_TIMEOUT_S, FILE_START_MARGIN_S, MODEL_FILE_BUDGET_S,
        )
        parsed = DOWNLOAD_TIMEOUT_S + EXTRACT_WALL_TIMEOUT_S + EMBED_FILE_BUDGET_S + FILE_START_MARGIN_S
        late = TICK_DEADLINE_S - parsed - MODEL_FILE_BUDGET_S / 2   # between the two worst cases
        assert can_start_file(late, routed=False) and not can_start_file(late, routed=True)


def test_records_keep_slice1_defaults():
    r = DriveIndexRecord(user_id="u", item_id="i")
    assert (r.lead_text, r.brief, r.triage_verdict, r.triage_version, r.triaged_content_version, r.caption) == \
        ("", None, None, 0, "", "")

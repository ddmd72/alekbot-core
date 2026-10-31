"""Content deferrals that survive re-scheduling (USER_DRIVE_INDEXING_RFC §8.2, §24.7, §24.8) and chunk collection
named by the observation plan (§3.4, §7.6). Pure."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.connection_scope import ConnectionScope
from src.domain import drive_index_policy as policy
from src.domain.drive_card import card_hash
from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus, TriageResult
from src.domain.drive_index import (
    ContentClass, DriveChange, DriveIndexRecord, EpochPhase, Priority, Reason, RecordState, Scope, TriageVerdict,
)
from src.domain.drive_index_params import QUIET, TICK_DEADLINE_S
from src.domain.drive_index_planner import (
    PlanContext, backoff, content_due, content_retry, deferred, generation_started, indexed, next_month_start,
    plan_observation, triage_due, triaged, with_card,
)
from src.domain.drive_index_policy import SLICE2_CONTENT_KINDS, SLICE2_TRIAGED_KINDS

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
ROOT = "root"


@pytest.fixture(autouse=True)
def admitted(monkeypatch):
    monkeypatch.setattr(policy, "ADMITTED_KINDS", SLICE2_CONTENT_KINDS)
    monkeypatch.setattr(policy, "TRIAGED_KINDS", SLICE2_TRIAGED_KINDS)


def ctx(now=NOW):
    return PlanContext(scope=ConnectionScope(user_id="u", connection_id="u"), root_item_id=ROOT, epoch=1, phase=EpochPhase.IDLE, now=now,
                       place_of=lambda lat, lon: None)


def ch(name="contract.pdf", cv="c1", parent=ROOT, item_id="a", **kw):
    return DriveChange(item_id=item_id, name=name, parent_id=parent, size_bytes=100, content_version=cv,
                       modified_at=NOW - timedelta(days=1), **kw)


def plan(change, existing=None, parent=None, now=NOW, **kw):
    return plan_observation(change, existing, parent, None, ctx(now), **kw)


def carded(rec, now=NOW):
    return with_card(rec, card_hash(rec), "s/8", now)


def first(change=None):
    return carded(plan(change or ch()).upsert)


def indexed_rec():
    out = ExtractionOutcome(status=ExtractionStatus.TEXT, text="Clause. " * 50, pages_total=1, extracted_by="p@1")
    return carded(indexed(generation_started(first(), NOW), outcome=out, generation=1, chunk_count=2, now=NOW))


class TestRetryBackoffSticks:
    def retrying(self):
        return content_retry(first(), Reason.EXTRACT_TIMEOUT, NOW)

    def test_retry_is_deferred(self):
        rec = self.retrying()
        assert (rec.due_at, rec.priority) == (NOW + backoff(1), Priority.RETRY)
        assert not content_due(rec, NOW) and content_due(rec, NOW + backoff(1))
        assert content_due(rec)   # the work exists; only its time is deferred

    def test_a_card_write_does_not_pull_it_forward(self):
        rec = carded(self.retrying())
        assert (rec.due_at, rec.priority) == (NOW + backoff(1), Priority.RETRY)

    def test_a_rename_does_not_pull_the_content_forward(self):
        rec = plan(ch("renamed.pdf"), self.retrying()).upsert
        assert (rec.due_at, rec.priority) == (NOW, Priority.EDIT)   # the card is due now ...
        assert not content_due(rec, NOW)                              # ... the content is not
        assert carded(rec).due_at == NOW + backoff(1)

    def test_new_content_restarts_the_work(self):
        rec = plan(ch(cv="c2"), self.retrying()).upsert
        assert (rec.defer_until, rec.defer_priority) == (None, None)
        assert rec.due_at == NOW - timedelta(days=1) + QUIET and content_due(rec, NOW)

    def test_completion_clears_it(self):
        out = ExtractionOutcome(status=ExtractionStatus.TEXT, text="x", extracted_by="p@1")
        rec = indexed(self.retrying(), outcome=out, generation=1, chunk_count=1, now=NOW)
        assert (rec.defer_until, rec.defer_priority) == (None, None)

    def test_a_started_run_waits_for_the_crash_window(self):
        run = carded(generation_started(first(), NOW))
        assert (run.due_at, run.priority) == (NOW + timedelta(seconds=TICK_DEADLINE_S), Priority.RETRY)
        assert not content_due(run, NOW)


class TestDeferred:
    def test_budget_waits_in_maintenance(self):
        until = next_month_start(NOW)
        rec = deferred(first(), until, NOW, maintenance=True)
        assert (rec.state, rec.due_at, rec.priority) == (RecordState.PENDING, until, Priority.MAINTENANCE)
        assert carded(rec).due_at == until and not content_due(rec, NOW) and content_due(rec, until)

    def test_outage_keeps_the_current_class(self):
        rec = replace(first(), priority=int(Priority.FIRST_SEEN), due_at=NOW)
        out = deferred(rec, NOW + timedelta(minutes=5), NOW)
        assert (out.due_at, out.priority) == (NOW + timedelta(minutes=5), Priority.FIRST_SEEN)
        assert plan(ch("renamed.pdf"), out).upsert.defer_until == NOW + timedelta(minutes=5)

    def test_triage_outage_ends_with_the_verdict(self):
        img = ch("IMG.jpg", item_id="img", mime_type="image/jpeg")
        rec = deferred(first(img), NOW + timedelta(hours=1), NOW)
        assert triage_due(rec) and not triage_due(rec, NOW)
        done = triaged(rec, TriageResult("img", TriageVerdict.TEXT), NOW)
        assert done.defer_until is None and done.due_at == NOW and content_due(done, NOW)

    @pytest.mark.parametrize("now,expected", [
        (NOW, datetime(2026, 11, 1, tzinfo=timezone.utc)),
        (datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc), datetime(2027, 1, 1, tzinfo=timezone.utc)),
        (datetime(2026, 11, 1, 0, 30, tzinfo=timezone(timedelta(hours=2))), datetime(2026, 11, 1, tzinfo=timezone.utc)),
    ])
    def test_next_month_start_is_utc(self, now, expected):
        assert next_month_start(now) == expected


class TestCollectChunks:
    def test_demotion_by_rename_collects(self):
        p = plan(ch(".contract.pdf"), indexed_rec())
        assert p.upsert.reason is Reason.DOT_NAME and p.collect_chunks == "a"

    def test_deleting_an_indexed_file_collects(self):
        p = plan(DriveChange(item_id="a", deleted=True), indexed_rec())
        assert (p.delete_item_id, p.collect_chunks) == ("a", "a")

    def test_deleting_after_a_crashed_run_collects(self):
        p = plan(DriveChange(item_id="a", deleted=True), generation_started(first(), NOW))
        assert p.collect_chunks == "a"

    def test_moving_into_an_opaque_folder_collects(self):
        repo = DriveIndexRecord(user_id="u", connection_id="u", item_id="repo", name="repo", parent_id=ROOT, is_folder=True,
                                kind="folder", content_class=ContentClass.FOLDER, scope=Scope.OPAQUE_ROOT,
                                state=RecordState.METADATA_ONLY, reason=Reason.OPAQUE_ROOT, policy_version=1)
        p = plan(ch(parent="repo"), indexed_rec(), parent=repo)
        assert (p.delete_item_id, p.collect_chunks) == ("a", "a")

    @pytest.mark.parametrize("change", [ch("renamed.pdf"), ch(cv="c2")])
    def test_a_readable_generation_that_stays_is_not_collected(self, change):
        assert plan(change, indexed_rec()).collect_chunks is None

    def test_never_read_records_collect_nothing(self, monkeypatch):
        monkeypatch.setattr(policy, "ADMITTED_KINDS", frozenset())
        rec = first()
        assert plan(DriveChange(item_id="a", deleted=True), rec).collect_chunks is None
        assert plan(ch(".x.pdf"), rec).collect_chunks is None

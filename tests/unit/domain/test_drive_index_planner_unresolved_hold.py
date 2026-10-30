"""`unresolved` is a scope hold for files (R3 F2-1, USER_DRIVE_INDEXING_RFC §3.4, §3.6, §8.2): a file whose parent
becomes unresolved keeps its content outcome, so when the parent resolves the outcome stands again — an indexed file
is indexed (never stuck `pending`), and a demoted or failed one is never read again. Pure."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.connection_scope import ConnectionScope
from src.domain import drive_index_policy as policy
from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import (
    POLICY_VERSION, STRUCTURE_POLICY_SINCE, DriveChange, DriveIndexRecord, EpochPhase, FolderProbe, Reason, RecordState,
    Scope,
)
from src.domain.drive_index_params import MAX_ATTEMPTS
from src.domain.drive_index_planner import (
    PlanContext, change_from_record, content_due, content_retry, demoted, generation_started, indexed,
    plan_observation, triage_due,
)
from src.domain.drive_index_policy import SLICE2_CONTENT_KINDS, SLICE2_TRIAGED_KINDS

NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)
ROOT = "root"


@pytest.fixture(autouse=True)
def admitted(monkeypatch):
    monkeypatch.setattr(policy, "ADMITTED_KINDS", SLICE2_CONTENT_KINDS)
    monkeypatch.setattr(policy, "TRIAGED_KINDS", SLICE2_TRIAGED_KINDS)


CTX = PlanContext(ConnectionScope("u", "u"), ROOT, 1, EpochPhase.IDLE, NOW, lambda a, b: None)


def folder(**kw) -> DriveIndexRecord:
    base = dict(user_id="u", item_id="F", parent_id=ROOT, name="Docs", is_folder=True, kind="folder",
                scope=Scope.NORMAL, state=RecordState.METADATA_ONLY, reason=Reason.FOLDER,
                policy_version=POLICY_VERSION)
    base.update(kw)
    return DriveIndexRecord(**base)


NORMAL = folder()
UNRESOLVED = folder(scope=Scope.UNRESOLVED, state=RecordState.UNRESOLVED, reason=None, probe_pending=True)


def pending_file(item_id: str = "X") -> DriveIndexRecord:
    change = DriveChange(item_id=item_id, parent_id="F", name=f"{item_id}.pdf", size_bytes=1000,
                         mime_type="application/pdf", content_version="c1", modified_at=NOW - timedelta(days=3))
    return plan_observation(change, None, NORMAL, None, CTX).upsert


def text_outcome() -> ExtractionOutcome:
    return ExtractionOutcome(status=ExtractionStatus.TEXT, text="hello world " * 50, extracted_by="parser@1")


def replan(rec: DriveIndexRecord, parent: DriveIndexRecord) -> DriveIndexRecord:
    plan = plan_observation(change_from_record(rec), rec, parent, None, CTX, parent, observed=False)
    assert plan.collect_chunks is None, "a scope hold never drops a readable generation"
    return plan.upsert or rec


def round_trip(rec: DriveIndexRecord) -> DriveIndexRecord:
    held = replan(rec, UNRESOLVED)
    assert (held.scope, held.state) == (Scope.UNRESOLVED, RecordState.UNRESOLVED)
    held_again = replan(held, UNRESOLVED)   # a re-plan while the parent is still unresolved keeps the hold
    return replan(held_again, NORMAL)


def indexed_file() -> DriveIndexRecord:
    run = generation_started(pending_file(), NOW)
    return indexed(run, outcome=text_outcome(), generation=run.index_generation, chunk_count=2, now=NOW,
                   chunk_space="s")


def test_an_indexed_file_is_indexed_again_after_its_parent_resolves():
    done = indexed_file()
    back = round_trip(done)
    assert (back.state, back.reason, back.indexed_generation) == (RecordState.INDEXED, None, done.indexed_generation)
    assert not content_due(back) and not triage_due(back)


@pytest.mark.parametrize("reason", [Reason.NO_TEXT, Reason.ENCRYPTED, Reason.MACHINE_TEXT, Reason.USER_CHUNK_CAP])
def test_a_content_demotion_stands_after_its_parent_resolves(reason):
    run = generation_started(pending_file(), NOW)
    dem = demoted(run, reason, NOW, outcome=text_outcome(), chunk_count=5)
    back = round_trip(dem)
    assert (back.state, back.reason) == (RecordState.METADATA_ONLY, reason)
    assert not content_due(back)
    if reason is Reason.USER_CHUNK_CAP:
        assert back.chunk_count == 5   # its release order still knows how many chunks it needs (§5.4)


def test_a_failed_file_stays_failed_after_its_parent_resolves():
    rec = generation_started(pending_file(), NOW)
    for _ in range(MAX_ATTEMPTS):
        rec = content_retry(rec, Reason.EXTRACT_ERROR, NOW)
    assert rec.state is RecordState.FAILED
    back = round_trip(rec)
    assert (back.state, back.reason) == (RecordState.FAILED, Reason.EXTRACT_ERROR)
    assert not content_due(back)


def test_a_pending_file_stays_pending_after_its_parent_resolves():
    back = round_trip(pending_file())
    assert back.state is RecordState.PENDING and content_due(back)


def test_a_new_content_version_under_the_hold_is_read_again():
    done = indexed_file()
    held = replan(done, UNRESOLVED)
    edited = replace(change_from_record(held), content_version="c2")
    back = plan_observation(edited, held, NORMAL, None, CTX, NORMAL).upsert
    assert back.state is RecordState.PENDING and content_due(back)


def test_a_policy_bump_replan_of_a_dense_folder_keeps_its_childrens_outcomes():
    """A structure-changing policy bump (a record older than STRUCTURE_POLICY_SINCE, Ruling T-R5) re-plans a folder of
    800 children without a probe: it becomes unresolved and rescopes its children, then its probe decides it is normal
    and rescopes them again. Every outcome survives (F2-1)."""
    big = folder(item_id="F", name="Scans", policy_version=STRUCTURE_POLICY_SINCE - 1, child_count=800,
                 probed_child_count=800, has_card=True)
    bumped = plan_observation(change_from_record(big), big, None, None, CTX, None, observed=False).upsert
    assert (bumped.scope, bumped.state, bumped.rescope_pending) == (Scope.UNRESOLVED, RecordState.UNRESOLVED, True)
    probe = FolderProbe(marker_found=False, class_counts={"document": 200}, sampled=200)
    resolved = plan_observation(change_from_record(bumped), bumped, None, probe, CTX, None, observed=False).upsert
    assert resolved.scope is Scope.NORMAL
    run = generation_started(pending_file("Y"), NOW)
    children = [indexed_file(), demoted(run, Reason.NO_TEXT, NOW)]
    for child in children:
        back = replan(replan(child, bumped), resolved)
        assert (back.state, back.reason, back.indexed_generation) == (child.state, child.reason,
                                                                     child.indexed_generation)
        assert not content_due(back)

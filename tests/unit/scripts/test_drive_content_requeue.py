"""The owner's requeue of failed content reads (USER_DRIVE_INDEXING_RFC §8.2, §3.4): selection, dry run, the lease,
the writes, and the whole path through the real indexer tick. In-memory repositories only: no network, no cloud write."""
import importlib.util
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import DriveIndexRecord, DriveSyncState, Priority, Reason, RecordState
from src.domain.drive_index_params import MAX_ATTEMPTS, RETRY_CAP
from src.domain.drive_index_planner import content_due

from tests.unit.services.drive_content_world import ContentWorld, admitted, prose  # noqa: F401  (fixture)
from tests.unit.services.drive_index_fakes import T0

_DIR = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure"
U = "user-1"


@pytest.fixture
def rq():
    spec = importlib.util.spec_from_file_location("drive_content_requeue", _DIR / "drive_content_requeue.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rec(item_id, kind="pdf", state=RecordState.FAILED, reason=Reason.EXTRACT_ERROR, **kw):
    return DriveIndexRecord(user_id=U, item_id=item_id, name=f"{item_id}.{kind}", kind=kind, content_version="c1",
                            state=state, reason=reason, has_card=True, card_hash="h", indexed_generation=1,
                            indexed_version="c0", extraction_version=1, **kw)


async def _repo(records):
    repo = InMemoryDriveIndexRepository()
    await repo.save_sync_state(DriveSyncState(user_id=U, epoch=1, root_item_id="root", created_at=T0))
    await repo.upsert_records(records)
    return repo


def _stored(repo):
    return {k: v for k, v in repo.records.items()}


def _mixed():
    return [_rec("item-1", "pdf"), _rec("item-2", "docx"), _rec("item-3", "pdf", reason=Reason.DOWNLOAD_ERROR),
            _rec("item-4", "pdf", state=RecordState.INDEXED, reason=None),
            _rec("item-5", "pdf", state=RecordState.METADATA_ONLY, reason=Reason.NO_TEXT),
            _rec("item-6", "pdf", state=RecordState.UNRESOLVED),
            _rec("item-7", "pdf", is_folder=True)]


# -- selection -------------------------------------------------------------------------------------------------------

def test_selection_by_reason_and_kind(rq):
    records = _mixed()
    by_reason = rq.build_plan(records, Reason.EXTRACT_ERROR, None, T0)
    assert [r.item_id for r in by_reason.records] == ["item-1", "item-2"]
    by_kind = rq.build_plan(records, Reason.EXTRACT_ERROR, "pdf", T0)
    assert [r.item_id for r in by_kind.records] == ["item-1"]
    other = rq.build_plan(records, Reason.DOWNLOAD_ERROR, None, T0)
    assert [r.item_id for r in other.records] == ["item-3"]


def test_the_plan_holds_the_records_as_the_planner_requeues_them(rq):
    plan = rq.build_plan(_mixed(), Reason.EXTRACT_ERROR, None, T0)
    for rec in plan.records:
        assert rec.state is RecordState.PENDING and content_due(rec, T0) and rq.reads_next_tick(rec, T0)
        assert (rec.due_at, rec.priority) == (T0, int(Priority.MAINTENANCE))
    assert plan.held == 0


def test_summary_counts_by_kind_and_reason_and_names_no_file(rq):
    plan = rq.build_plan(_mixed(), Reason.EXTRACT_ERROR, None, T0)
    text = rq.summary(plan)
    assert "2 failed files" in text and "docx/extract_error: 1" in text and "pdf/extract_error: 1" in text
    assert "item-" not in text and ".pdf" not in text and "held" not in text


def test_a_record_that_stays_held_is_counted(rq):
    plan = rq.build_plan([_rec("item-1", card_capped=True), _rec("item-2")], Reason.EXTRACT_ERROR, None, T0)
    assert plan.held == 1 and len(plan.records) == 2
    assert "1 of them stay held" in rq.summary(plan)


def test_the_reason_choices_are_the_per_file_failures(rq):
    assert rq.REASONS == ("download_error", "embed_timeout", "extract_error", "extract_memory", "extract_timeout")


# -- dry run, lease, apply -------------------------------------------------------------------------------------------

async def test_dry_run_writes_nothing(rq):
    repo = await _repo(_mixed())
    before = _stored(repo)
    code, message = await rq.requeue(repo, U, reason=Reason.EXTRACT_ERROR, kind=None, apply=False, now=T0, owner="r")
    assert code == 0 and message.startswith("dry run: 2 failed files") and "--apply" in message
    assert _stored(repo) == before and (await repo.get_sync_state(U)).lease_owner is None


async def test_apply_writes_exactly_the_selected_records_and_releases_the_lease(rq):
    repo = await _repo(_mixed())
    before = _stored(repo)
    code, message = await rq.requeue(repo, U, reason=Reason.EXTRACT_ERROR, kind="pdf", apply=True, now=T0, owner="r")
    after = _stored(repo)
    assert code == 0 and message.startswith("requeued: 1 failed files")
    changed = {k[1] for k in after if after[k] != before[k]}
    assert changed == {"item-1"} and after.keys() == before.keys()
    rec = after[(U, "item-1")]
    assert rec.state is RecordState.PENDING and content_due(rec, T0) and rec.indexed_generation == 1
    assert (await repo.get_sync_state(U)).lease_owner is None
    assert [r.item_id for r in await repo.due_records(U, T0, 10)] == ["item-1"]


async def test_a_busy_lease_writes_nothing(rq):
    repo = await _repo(_mixed())
    await repo.acquire_lease(U, "tick", T0, T0 + timedelta(minutes=30), create=False)
    before = _stored(repo)
    code, message = await rq.requeue(repo, U, reason=Reason.EXTRACT_ERROR, kind=None, apply=True, now=T0, owner="r")
    assert code == 1 and "retry" in message and _stored(repo) == before
    assert (await repo.get_sync_state(U)).lease_owner == "tick"


async def test_apply_selects_again_under_the_lease(rq, monkeypatch):
    repo = await _repo([_rec("item-1"), _rec("item-2")])
    taken = repo.acquire_lease

    async def a_tick_reads_item_2_first(*args, **kw):
        state = await taken(*args, **kw)
        await repo.upsert_records([replace(repo.records[(U, "item-2")], state=RecordState.INDEXED, reason=None)])
        return state

    monkeypatch.setattr(repo, "acquire_lease", a_tick_reads_item_2_first)
    code, message = await rq.requeue(repo, U, reason=Reason.EXTRACT_ERROR, kind=None, apply=True, now=T0, owner="r")
    assert code == 0 and "1 failed files" in message
    assert repo.records[(U, "item-2")].state is RecordState.INDEXED


@pytest.mark.parametrize("apply", [False, True])
async def test_nothing_to_requeue(rq, apply):
    repo = await _repo([_rec("item-4", state=RecordState.INDEXED, reason=None)])
    before = _stored(repo)
    code, message = await rq.requeue(repo, U, reason=Reason.EXTRACT_ERROR, kind=None, apply=apply, now=T0, owner="r")
    assert (code, message) == (0, "nothing to requeue") and _stored(repo) == before
    assert (await repo.get_sync_state(U)).lease_owner is None


async def test_nothing_indexed_is_an_error(rq):
    code, _ = await rq.requeue(InMemoryDriveIndexRepository(), U, reason=Reason.EXTRACT_ERROR, kind=None, apply=True,
                               now=T0, owner="r")
    assert code == 1


async def test_a_reason_that_is_not_a_per_file_failure_is_refused(rq):
    with pytest.raises(ValueError):
        await rq.requeue(await _repo([]), U, reason=Reason.NO_TEXT, kind=None, apply=False, now=T0, owner="r")


async def test_the_scan_pages_through_the_index_and_writes_in_batches(rq, monkeypatch):
    monkeypatch.setattr(rq, "_PAGE", 2)
    monkeypatch.setattr(rq, "_WRITE_BATCH", 2)
    repo = await _repo([_rec(f"item-{i}") for i in range(1, 6)])
    code, message = await rq.requeue(repo, U, reason=Reason.EXTRACT_ERROR, kind=None, apply=True, now=T0, owner="r")
    assert code == 0 and "5 failed files" in message
    assert all(r.state is RecordState.PENDING for r in repo.records.values())


# -- the whole path through the real tick ----------------------------------------------------------------------------

@pytest.mark.usefixtures("admitted")
async def test_a_failed_file_is_read_again_by_the_next_tick_only_after_the_requeue(rq):
    w = ContentWorld()
    name = "Tom's plan.docx"   # parser-only: a pdf the parser fails goes to the reader instead
    doc = w.add_doc(name, prose(40))
    w.parser.outcomes[name] = ExtractionOutcome(status=ExtractionStatus.ERROR)
    for _ in range(MAX_ATTEMPTS):
        await w.settle()
        w.clock.advance(RETRY_CAP.total_seconds())
    rec = w.rec(doc)
    assert (rec.state, rec.reason) == (RecordState.FAILED, Reason.EXTRACT_ERROR)
    reads = len(w.parser.requests)
    assert reads == MAX_ATTEMPTS

    del w.parser.outcomes[name]   # the cause is fixed: the parser reads the file now
    w.clock.advance(RETRY_CAP.total_seconds())
    await w.settle()
    assert w.rec(doc).state is RecordState.FAILED and len(w.parser.requests) == reads   # the outcome stands

    code, _ = await rq.requeue(w.repo, "u", reason=Reason.EXTRACT_ERROR, kind="docx", apply=True, now=w.clock.now,
                               owner="r")
    assert code == 0
    await w.settle()
    rec = w.rec(doc)
    assert (rec.state, rec.reason, rec.attempts) == (RecordState.INDEXED, None, 0)
    assert len(w.parser.requests) == reads + 1 and w.readable(doc)

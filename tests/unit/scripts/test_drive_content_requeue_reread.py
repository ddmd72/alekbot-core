"""The owner's re-read of truncated reads and of text images with no text (`--truncated`, `--no-text`;
USER_DRIVE_INDEXING_RFC §5.4, §24.5): selection, the dry run's counts, `--apply` writing only the selection, the CLI,
and the whole path through the real indexer tick. In-memory repositories only: no network, no cloud write."""
import importlib.util
from datetime import timedelta
from pathlib import Path

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import (
    TRIAGE_VERSION, DriveIndexRecord, DriveSyncState, Priority, Reason, RecordState, TriageVerdict,
)
from src.domain.drive_index_planner import content_due, triage_due

from tests.unit.services.drive_content_world import ContentWorld, admitted, prose  # noqa: F401  (fixture)
from tests.unit.services.drive_index_fakes import T0

_DIR = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure"
U = "user-1"

pytestmark = pytest.mark.usefixtures("admitted")


@pytest.fixture
def rq():
    spec = importlib.util.spec_from_file_location("drive_content_requeue", _DIR / "drive_content_requeue.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rec(item_id, kind="pdf", state=RecordState.INDEXED, reason=None, truncated=True, **kw):
    fields = dict(name=f"{item_id}.{kind}", has_card=True, card_hash="h", indexed_generation=1, indexed_version="c1",
                  extraction_version=1, chunk_count=12, extracted_by="parser@3")
    fields.update(kw)
    return DriveIndexRecord(user_id=U, item_id=item_id, kind=kind, content_version="c1",
                            state=state, reason=reason, truncated=truncated, **fields)


def _image(item_id, verdict=TriageVerdict.TEXT, **kw):
    fields = dict(state=RecordState.METADATA_ONLY, reason=Reason.NO_TEXT, truncated=False, indexed_generation=None,
                  chunk_count=0, extracted_by=None, triage_verdict=verdict, triage_version=TRIAGE_VERSION,
                  triaged_content_version="c1")
    fields.update(kw)
    return _rec(item_id, "image", **fields)


async def _repo(records):
    repo = InMemoryDriveIndexRepository()
    await repo.save_sync_state(DriveSyncState(user_id=U, epoch=1, root_item_id="root", created_at=T0))
    await repo.upsert_records(records)
    return repo


def _mixed():
    return [_rec("item-1", "pdf"), _rec("item-2", "docx"), _rec("item-3", "pdf", truncated=False),
            _rec("item-4", "pdf", state=RecordState.FAILED, reason=Reason.EXTRACT_ERROR),
            _rec("item-5", "pdf", state=RecordState.METADATA_ONLY, reason=Reason.NO_TEXT, truncated=False,
                 indexed_generation=None),
            _image("item-6"), _image("item-7", verdict=TriageVerdict.PHOTO, reason=Reason.PHOTOGRAPH),
            _image("item-8", triage_version=0), _rec("item-9", "pdf", is_folder=True),
            _rec("item-10", "pdf", name="resume.pdf"), _image("item-11", name="screenshot_train_es.png")]


# -- selection -------------------------------------------------------------------------------------------------------

def test_truncated_selects_only_indexed_truncated_reads(rq):
    plan = rq.build_plan(_mixed(), None, None, T0, rq.TRUNCATED)
    assert [r.item_id for r in plan.records] == ["item-1", "item-10", "item-2"]
    assert [r.item_id for r in rq.build_plan(_mixed(), None, "docx", T0, rq.TRUNCATED).records] == ["item-2"]


def test_no_text_selects_only_text_images_whose_verdict_holds(rq):
    plan = rq.build_plan(_mixed(), None, None, T0, rq.NO_TEXT)
    assert [r.item_id for r in plan.records] == ["item-11", "item-6"]


@pytest.mark.parametrize("select", ["truncated", "no_text"])
def test_the_plan_holds_the_records_as_the_planner_re_reads_them(rq, select):
    plan = rq.build_plan(_mixed(), None, None, T0, select)
    assert plan.records and plan.held == 0
    for rec in plan.records:
        assert rec.state is RecordState.PENDING and content_due(rec, T0) and rq.reads_next_tick(rec, T0)
        assert not triage_due(rec, T0) and (rec.due_at, rec.priority) == (T0, int(Priority.MAINTENANCE))
        assert rec.has_card and rec.card_hash == "h"


def test_a_card_capped_image_is_counted_as_held(rq):
    plan = rq.build_plan([_image("item-1", card_capped=True), _image("item-2")], None, None, T0, rq.NO_TEXT)
    assert plan.held == 1 and len(plan.records) == 2


def test_summaries_count_by_kind_name_no_file_and_count_the_reader(rq):
    truncated = rq.summary(rq.build_plan(_mixed(), None, None, T0, rq.TRUNCATED))
    assert truncated == "3 truncated files (docx/truncated: 1, pdf/truncated: 2)"
    images = rq.summary(rq.build_plan(_mixed(), None, None, T0, rq.NO_TEXT))
    assert images == "2 images with no text (image/no_text: 2); 2 of them go to the reader (billed)"
    scan = rq.build_plan([_rec("item-1", extracted_by="reader@2"), _rec("item-2")], None, None, T0, rq.TRUNCATED)
    assert scan.reader == 1 and "item-" not in rq.summary(scan) and ".pdf" not in rq.summary(scan)


# -- dry run, apply, refusals ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("select, first", [("truncated", "dry run: 3 truncated files"),
                                           ("no_text", "dry run: 2 images with no text")])
async def test_dry_run_writes_nothing(rq, select, first):
    repo = await _repo(_mixed())
    before = dict(repo.records)
    code, message = await rq.requeue(repo, U, reason=None, kind=None, apply=False, now=T0, owner="r", select=select)
    assert code == 0 and message.startswith(first) and "--apply" in message
    assert dict(repo.records) == before and (await repo.get_sync_state(U)).lease_owner is None


@pytest.mark.parametrize("select, selected", [("truncated", {"item-1", "item-2", "item-10"}),
                                              ("no_text", {"item-6", "item-11"})])
async def test_apply_writes_exactly_the_selected_records_and_releases_the_lease(rq, select, selected):
    repo = await _repo(_mixed())
    before = dict(repo.records)
    code, message = await rq.requeue(repo, U, reason=None, kind=None, apply=True, now=T0, owner="r", select=select)
    after = dict(repo.records)
    assert code == 0 and message.startswith("requeued: ")
    assert {k[1] for k in after if after[k] != before[k]} == selected and after.keys() == before.keys()
    assert {r.item_id for r in await repo.due_records(U, T0, 10)} == selected
    assert (await repo.get_sync_state(U)).lease_owner is None


@pytest.mark.parametrize("select", ["truncated", "no_text"])
async def test_a_failure_reason_with_a_re_read_selection_is_refused(rq, select):
    with pytest.raises(ValueError):
        await rq.requeue(await _repo([]), U, reason=Reason.EXTRACT_ERROR, kind=None, apply=False, now=T0, owner="r",
                         select=select)


async def test_a_busy_lease_writes_nothing(rq):
    repo = await _repo(_mixed())
    await repo.acquire_lease(U, "tick", T0, T0 + timedelta(minutes=30), create=False)
    before = dict(repo.records)
    code, message = await rq.requeue(repo, U, reason=None, kind=None, apply=True, now=T0, owner="r",
                                     select=rq.TRUNCATED)
    assert code == 1 and "retry" in message and dict(repo.records) == before


# -- the command line -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("argv, expected", [
    ([], ("failed", Reason.EXTRACT_ERROR)),
    (["--reason", "download_error"], ("failed", Reason.DOWNLOAD_ERROR)),
    (["--truncated"], ("truncated", None)),
    (["--no-text", "--kind", "image"], ("no_text", None)),
])
def test_the_cli_takes_one_selection(rq, argv, expected):
    assert rq.selection_of(rq.parser().parse_args(["--user", U, *argv])) == expected


@pytest.mark.parametrize("argv", [["--truncated", "--no-text"], ["--reason", "extract_error", "--truncated"],
                                  ["--reason", "no_text"]])
def test_the_cli_refuses_two_selections_or_a_reason_that_is_no_failure(rq, argv):
    with pytest.raises(SystemExit):
        rq.parser().parse_args(["--user", U, *argv])


# -- the whole path through the real tick ----------------------------------------------------------------------------

async def test_a_truncated_read_is_read_in_full_by_the_next_tick_only_after_the_re_read(rq):
    w = ContentWorld()
    name = "Tom's plan.docx"
    doc = w.add_doc(name, prose(120))
    w.parser.outcomes[name] = ExtractionOutcome(status=ExtractionStatus.TEXT, text=prose(40), truncated=True)
    await w.settle()
    rec = w.rec(doc)
    assert (rec.state, rec.truncated) == (RecordState.INDEXED, True)
    first, reads = rec.indexed_generation, len(w.parser.requests)
    cut = len(w.readable(doc))

    del w.parser.outcomes[name]   # the cap is raised: the parser reads the whole file now
    w.clock.advance(3600)
    await w.settle()
    assert w.rec(doc).truncated and len(w.parser.requests) == reads   # the outcome stands

    code, _ = await rq.requeue(w.repo, "u", reason=None, kind="docx", apply=True, now=w.clock.now, owner="r",
                               select=rq.TRUNCATED)
    assert code == 0
    await w.settle()
    rec = w.rec(doc)
    assert (rec.state, rec.truncated, rec.attempts) == (RecordState.INDEXED, False, 0)
    assert len(w.parser.requests) == reads + 1 and rec.indexed_generation != first
    assert len(w.readable(doc)) > cut and w.generations(doc) == [rec.indexed_generation]


async def test_a_text_image_with_no_text_goes_to_the_reader_again_and_is_not_triaged_again(rq):
    w = ContentWorld()
    w.triage.default = TriageVerdict.TEXT
    w.reader.outcomes["garage-receipt.png"] = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=1)
    img = w.add_image("garage-receipt.png")
    await w.settle()
    assert (w.rec(img).state, w.rec(img).reason) == (RecordState.METADATA_ONLY, Reason.NO_TEXT)
    triage_calls, reads = len(w.triage.calls), len(w.reader.requests)
    assert reads == 1

    w.reader.outcomes["garage-receipt.png"] = ExtractionOutcome(
        status=ExtractionStatus.TEXT, text="Garage receipt: car service, oil change and the logbook stamped.",
        pages_total=1, pages_sent=1, model_calls=1)   # the fix: the reader gets a rendition it can read
    w.clock.advance(3600)
    await w.settle()
    assert w.rec(img).reason is Reason.NO_TEXT and len(w.reader.requests) == reads   # the outcome stands

    code, message = await rq.requeue(w.repo, "u", reason=None, kind=None, apply=True, now=w.clock.now, owner="r",
                                     select=rq.NO_TEXT)
    assert code == 0 and "1 images with no text" in message
    await w.settle()
    rec = w.rec(img)
    assert (rec.state, rec.reason, rec.triage_verdict) == (RecordState.INDEXED, None, TriageVerdict.TEXT)
    assert len(w.reader.requests) == reads + 1 and len(w.triage.calls) == triage_calls and w.readable(img)

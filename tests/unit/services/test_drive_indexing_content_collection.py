"""Chunks leave with their content (USER_DRIVE_INDEXING_RFC §3.4, §7.5, §7.6, §17.1 (e)): deletion, demotion,
cascades, the purge and the sweep's chunk backstop."""
import pytest

from src.domain.drive_index import DriveChunk, RecordState

from .drive_content_world import ContentWorld, admitted, prose  # noqa: F401  (fixture)
from .drive_index_fakes import U_SCOPE

pytestmark = pytest.mark.usefixtures("admitted")


@pytest.fixture
def w():
    return ContentWorld()


async def indexed_doc(w, name="a.md", parent="root"):
    doc = w.add_doc(name, prose(150), parent=parent)
    await w.settle()
    assert w.readable(doc)
    return doc


async def debris(w, item_id, generation, n=2):
    await w.repo.write_chunks([DriveChunk("u", item_id, generation, i, "stray", 0, 5, "markdown", connection_id="u") for i in range(n)],
                              [[0.1] * 8 for _ in range(n)])


async def test_a_deleted_file_takes_its_chunks(w):
    doc = await indexed_doc(w)
    w.drive.delete(doc)
    await w.settle()
    assert w.rec(doc) is None and w.chunks(doc) == []


async def test_a_rename_into_a_hidden_name_collects_the_chunks(w):
    doc = await indexed_doc(w)
    w.drive.rename(doc, ".a.md")
    await w.settle()
    rec = w.rec(doc)
    assert (rec.state, rec.indexed_generation) == (RecordState.METADATA_ONLY, None) and w.chunks(doc) == []


async def test_a_move_into_a_repository_collects_the_chunks(w):
    doc = await indexed_doc(w)
    repo = w.drive.add("repo", folder=True)
    w.drive.add(".git", parent=repo, folder=True)
    await w.settle()
    w.drive.move(doc, repo)
    await w.settle()
    assert w.rec(doc) is None and w.chunks(doc) == []


async def test_a_folder_delete_cascade_collects_file_chunks(w):
    folder = w.drive.add("Contracts", folder=True)
    doc = await indexed_doc(w, parent=folder)
    w.drive.log.append(w.drive.items[folder].__class__(item_id=folder, deleted=True))   # the folder's tombstone only
    await w.settle()
    assert w.rec(doc) is None and w.chunks(doc) == []


async def test_opt_out_purges_chunks_records_and_state(w):
    await indexed_doc(w)
    await indexed_doc(w, "b.md")
    w.users.enabled["u"] = False
    await w.settle()
    assert w.repo.chunks == {} and w.repo.records == {} and w.repo.states == {}
    assert w.summary_total("purged_chunks") > 0


async def test_the_sweep_removes_orphan_and_unreadable_chunks(w):
    doc = await indexed_doc(w)
    await debris(w, "ghost", 1)            # no record
    await debris(w, doc, 7)                # not the readable generation
    w.drive.resync_next = True             # a 410: an enumeration, then its sweep (§7.5)
    await w.settle()
    assert ("u", "ghost", 1, 0) not in w.repo.chunks and w.generations(doc) == [1] and w.readable(doc)
    assert w.summary_total("swept_chunks") == 4


async def test_a_swept_record_takes_its_chunks(w):
    doc = await indexed_doc(w)
    w.drive.items.pop(doc)                 # gone from the drive without a tombstone: only an enumeration sees it
    w.drive.resync_next = True
    await w.settle()
    assert w.rec(doc) is None and w.chunks(doc) == []


async def test_the_chunk_backstop_restarts_when_cut(w, monkeypatch):
    from src.domain.drive_index_params import TICK_START_BUDGET
    from src.services import drive_indexing_service as svc_mod
    doc = await indexed_doc(w)
    for g in range(2, 6):
        await debris(w, doc, g, n=3)
    monkeypatch.setattr(svc_mod, "RECORD_WRITE_BATCH", 4)
    starts = []
    real = w.repo.chunks_page

    async def chunks_page(user_id, after, limit):
        starts.append(after)
        if len(starts) == 1:   # the first page eats the tick's start budget
            w.clock.advance(TICK_START_BUDGET.total_seconds())
        return await real(user_id, after, limit)
    w.repo.chunks_page = chunks_page
    w.drive.resync_next = True
    await w.settle()
    assert starts.count(None) == 2   # cut after one page, then one whole pass from the first chunk
    assert w.generations(doc) == [1] and (await w.repo.get_sync_state(U_SCOPE)).epoch_phase.value == "idle"

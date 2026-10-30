"""Hint documents leave with their chunks, first (USER_DRIVE_INDEXING_RFC §25.2.2 Lifecycle): wherever a generation is
collected the item's hint documents go before its chunks (`delete_hints_except`, then `delete_chunks_except`); the
purge (opt-out, access loss) deletes every hint document first (`delete_user_hints`); the sweep pages hint documents
as well as chunks, so a hint document whose chunks are already gone is still found. The usage documents survive a
purge (§25.4.5)."""
from datetime import timedelta

import pytest

from src.domain.drive_enrichment import DriveHintDoc, HintItem

from .drive_content_world import ContentWorld, admitted, prose  # noqa: F401  (fixture)
from .drive_index_fakes import U_SCOPE

pytestmark = pytest.mark.usefixtures("admitted")


@pytest.fixture
def w():
    world = ContentWorld()
    world.calls = []
    for name in ("delete_hints_except", "delete_chunks_except", "delete_user_hints", "delete_user_chunks",
                 "delete_user_records"):
        real = getattr(world.repo, name)

        async def logged(*args, _real=real, _name=name):
            world.calls.append((_name, args[1] if len(args) > 2 else None))
            return await _real(*args)
        setattr(world.repo, name, logged)
    return world


def hints_of(w, item_id):
    return sorted((k[2], k[3]) for k in w.repo.hints if k[0] == "u" and k[1] == item_id)


async def put_hints(w, item_id, generation, n=2):
    docs = [DriveHintDoc("u", item_id, generation, i, "markdown", f"h{i}", (HintItem("rent", "keyword", "tenant"),),
                         ("w:rent",), 1) for i in range(n)]
    await w.repo.write_hints(docs, [[0.1] * 8 for _ in docs])


async def indexed_doc(w, name="a.md"):
    doc = w.add_doc(name, prose(150))
    await w.settle()
    assert w.readable(doc)
    return doc


def hints_before_chunks(w, item_id):
    """Every chunk collection of the item follows a hint collection of the same item."""
    calls = [name for name, item in w.calls if item == item_id]
    for i, name in enumerate(calls):
        if name == "delete_chunks_except":
            assert i > 0 and calls[i - 1] == "delete_hints_except", calls
    return calls


async def test_a_deleted_file_takes_its_hints_first(w):
    doc = await indexed_doc(w)
    await put_hints(w, doc, w.rec(doc).indexed_generation)
    w.drive.delete(doc)
    await w.settle()
    assert w.rec(doc) is None and hints_of(w, doc) == [] and w.chunks(doc) == []
    assert "delete_chunks_except" in hints_before_chunks(w, doc)


async def test_an_edit_collects_the_old_generations_hints(w):
    doc = await indexed_doc(w)
    old = w.rec(doc).indexed_generation
    await put_hints(w, doc, old)
    w.drive.edit(doc, prose(160, "rewritten").encode("utf-8"), modified_at=w.clock.now - timedelta(hours=1))
    await w.settle()
    new = w.rec(doc).indexed_generation
    assert new != old and w.generations(doc) == [new]
    assert hints_of(w, doc) == []
    hints_before_chunks(w, doc)


async def test_opt_out_purges_every_hint_document_first(w):
    a = await indexed_doc(w)
    b = await indexed_doc(w, "b.md")
    await put_hints(w, a, w.rec(a).indexed_generation)
    await put_hints(w, b, 99)                                  # debris of a generation that never committed
    await w.repo.add_model_usage("u", "2026-10", "2026-10-13", "profile", 0.5, 0.0, 1)
    await w.repo.spend_initial_build("u", 1.0, w.clock.now)
    w.users.enabled["u"] = False
    await w.settle()
    assert w.repo.hints == {} and w.repo.chunks == {} and w.repo.records == {} and w.repo.states == {}
    purge = [name for name, _ in w.calls if name.startswith("delete_user_")]
    assert purge.index("delete_user_hints") < purge.index("delete_user_chunks") < purge.index("delete_user_records")
    assert w.summary_total("purged_hints") == 4
    assert (await w.repo.get_model_usage("u", "2026-10")).profile_usd == 0.5   # the purge keeps the usage documents
    assert (await w.repo.get_initial_build("u")).spent_usd == 1.0


async def test_the_sweep_finds_hint_documents_whose_chunks_are_gone(w):
    doc = await indexed_doc(w)
    readable = w.rec(doc).indexed_generation
    await put_hints(w, doc, readable)                          # kept: the readable generation
    await put_hints(w, doc, readable + 5)                      # no chunks, not readable
    await put_hints(w, "ghost", 1, n=3)                        # no record, no chunks
    w.drive.resync_next = True                                 # a 410: an enumeration, then its sweep (§7.5)
    await w.settle()
    assert hints_of(w, doc) == [(readable, 0), (readable, 1)] and hints_of(w, "ghost") == []
    assert w.readable(doc)
    assert w.summary_total("swept_hints") == 5


async def test_the_hint_sweep_restarts_when_cut(w, monkeypatch):
    from src.domain.drive_index_params import TICK_START_BUDGET
    from src.services import drive_indexing_service as svc_mod
    doc = await indexed_doc(w)
    for g in range(2, 6):
        await put_hints(w, doc, g + 10, n=3)
    monkeypatch.setattr(svc_mod, "RECORD_WRITE_BATCH", 4)
    starts = []
    real = w.repo.hints_page

    async def hints_page(user_id, after, limit):
        starts.append(after)
        if len(starts) == 1:   # the first page eats the tick's start budget
            w.clock.advance(TICK_START_BUDGET.total_seconds())
        return await real(user_id, after, limit)
    w.repo.hints_page = hints_page
    w.drive.resync_next = True
    await w.settle()
    assert starts.count(None) == 2   # cut after one page, then one whole pass from the first hint document
    assert hints_of(w, doc) == [] and (await w.repo.get_sync_state(U_SCOPE)).epoch_phase.value == "idle"

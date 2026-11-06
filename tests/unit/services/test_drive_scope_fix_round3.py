"""Third round of fixes after the review of the scope diff: deferred paid work is due later and holds nothing back,
many new roots with no record are read over ticks, the stored path links are only the walked ones, the skeleton scan
flag stays at its cap, and a folder moved out of the scope keeps its new parent. Synthetic items only."""
from dataclasses import replace

from src.domain.drive_feed_scope import FeedScope
from src.domain.drive_index import DriveIndexRecord, Priority, RecordState
from src.domain.drive_index_planner import schedule
from src.domain.drive_index_scope import IndexScope
from src.services import drive_indexing_service as svc_mod
from src.services.drive_indexing_service import _Budget, _ScopeView, _TICK_SCOPE

from .test_drive_scope_diff_tick import SCOPE, build, state_of
from .test_drive_scope_fix_batch import cont_tasks


async def pending_many(w, items):
    out = []
    for item in items:
        rec = (await w.repo.get_records(w.handle.scope, [item]))[item]
        out.append(schedule(replace(rec, state=RecordState.PENDING, indexed_generation=None, has_card=False,
                                    card_hash=None), w.clock(), first_seen=True))
    await w.repo.upsert_records(out)


async def crowded(count=105):
    w = await build(FeedScope.ACCOUNT)
    files = [w.plant(w.coimbra, f"Day {i}.txt") for i in range(count)]
    await w.scope(folders=[w.rd, w.coimbra])
    await w.settle()
    await pending_many(w, files)
    return w, files


async def test_more_deferred_records_than_a_page_request_no_continuation():
    w, files = await crowded()
    before = cont_tasks(w)
    reader = w.svc._extractors["parser"]
    seen = len(reader.requests)
    await w.scope(folders=[w.rd])
    for i in range(3):
        w.plant(w.springfield, f"Archive {i}", True)      # the guard stays shut
        await w.svc.tick(SCOPE)
        w.clock.advance(60)
    assert cont_tasks(w) == before
    assert not {r.item_id for r in reader.requests[seen:]} & set(files)


async def test_an_in_scope_first_seen_file_is_indexed_behind_more_deferred_records_than_a_page():
    w, files = await crowded()
    w.clock.advance(120)                                      # the deferred records have been due for a while
    w.plant(w.springfield, "Spring notes.txt")
    await w.scope(folders=[w.rd, w.springfield])             # Coimbra goes, Springfield is walked
    w.plant(w.projects, "Archive 1", True)                    # the guard is shut: the removal waits
    await w.svc.tick(SCOPE)
    assert w.names()["Spring notes.txt"].state is RecordState.INDEXED


async def test_new_roots_with_no_record_beyond_a_tick_of_item_calls_are_read_over_ticks(monkeypatch):
    w = await build(FeedScope.ACCOUNT)
    roots = [w.plant(w.root, f"Trip {i}", True) for i in range(25)]      # seen by the feed, out of scope: no record
    await w.scope(folders=[w.rd])
    await w.settle()
    original = w.drive.get_change
    reads = []

    async def counting(handle, item_id):
        reads.append(item_id)
        return await original(handle, item_id)

    w.drive.get_change = counting
    monkeypatch.setattr(svc_mod, "ITEM_CALLS_PER_TICK", 5)
    await w.scope(folders=[w.rd] + roots)
    key = (await w.connections.get("usr-1", "conn-1")).scope_key
    for _ in range(20):
        await w.svc.tick(SCOPE)
        w.clock.advance(60)
        state = await state_of(w)
        if state.applied_scope == key:
            break
    assert (await state_of(w)).applied_scope == key and (await state_of(w)).diff_parents == ()
    assert max(reads.count(r) for r in roots) <= 2            # once for the parent map, once for the walk


async def test_an_incomplete_ancestor_load_stores_only_the_links_it_walked(monkeypatch):
    w = await build(FeedScope.ACCOUNT)
    parent, chain = w.root, []
    for i in range(6):
        parent = w.plant(parent, f"Level {i}", True)
        chain.append(parent)
    await w.scope(folders=[chain[-1]])
    await w.settle()
    await w.repo.delete_records(w.handle.scope, chain[:-1])
    state = await state_of(w)
    conn = await w.connections.get("usr-1", "conn-1")
    view = _ScopeView(IndexScope.of(conn, await w.drive.capabilities(w.handle)))
    stray = await w.drive.get_change(w.handle, w.springfield)    # a folder a page's failed verdict read happened to fetch
    view.reads[w.springfield] = stray
    token = _TICK_SCOPE.set(view)
    try:
        budget = _Budget(started=0.0, monotonic=w.clock.monotonic)
        budget.item_calls = 2
        await w.svc._load_scope_ancestors(w.handle, view, w.svc._ctx(w.handle, state, w.clock()), budget)
    finally:
        _TICK_SCOPE.reset(token)
    assert not view.loaded
    assert "Springfield" not in w.names()                         # never walked: not a skeleton
    assert "Level 4" in w.names() and "Level 3" in w.names()      # the walked links are


async def test_the_skeleton_flag_stays_when_the_scan_hit_its_cap(monkeypatch):
    w = await build(FeedScope.ACCOUNT)
    await w.scope(folders=[w.acme])
    await w.settle()
    state = await state_of(w)
    await w.repo.save_sync_state(replace(state, ancestry_dirty=True))
    monkeypatch.setattr(svc_mod, "_SKELETON_SCAN", 1)
    await w.svc.tick(SCOPE)
    assert (await state_of(w)).ancestry_dirty


async def test_a_folder_that_leaves_the_scope_by_a_move_is_not_revived_by_its_old_parent():
    w = await build(FeedScope.ACCOUNT)
    await w.scope(folders=[w.rd])
    await w.settle()
    await w.drive.move(w.handle, w.hamburg, w.springfield)
    w.drive.touch(w.handle, w.hamburg_notes, edited=True)
    await w.settle()
    assert "Hamburg" not in w.names() and "Hamburg notes.txt" not in w.names()

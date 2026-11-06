"""Third round of fixes after the review of the scope diff: many new roots with no record are read over ticks, the stored
path links are only the walked ones, and a folder moved out of the scope keeps its new parent. Synthetic items only."""

from src.domain.drive_feed_scope import FeedScope
from src.domain.drive_index_scope import IndexScope
from src.services import drive_indexing_service as svc_mod
from src.services.drive_indexing_service import _Budget, _ScopeView, _TICK_SCOPE

from .test_drive_scope_diff_tick import SCOPE, build, state_of


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


async def test_a_folder_that_leaves_the_scope_by_a_move_is_not_revived_by_its_old_parent():
    w = await build(FeedScope.ACCOUNT)
    await w.scope(folders=[w.rd])
    await w.settle()
    await w.drive.move(w.handle, w.hamburg, w.springfield)
    w.drive.touch(w.handle, w.hamburg_notes, edited=True)
    await w.settle()
    assert "Hamburg" not in w.names() and "Hamburg notes.txt" not in w.names()

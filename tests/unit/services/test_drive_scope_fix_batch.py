"""Fixes after the review of the scope diff (DRIVE_SCOPE_WALK_RFC step 1): the removing line ends in the tick that ends
the removal, a waiting guard does not chain ticks, the top-level pass keeps its place, a long parent map is read over
ticks, a waiting cascade reads nothing, paid work waits under a root being removed, and two rollback/Cabinet hygiene
points. The real service on the in-memory repositories and the stateful fake drive. Synthetic items only."""
from dataclasses import replace

from src.domain.drive_feed_scope import FeedScope
from src.domain.drive_index import RecordState
from src.domain.drive_index_planner import schedule
from src.services import drive_indexing_service as svc_mod

from .test_drive_scope_diff_tick import SCOPE, build, state_of


def cont_tasks(w) -> int:
    return len([t for t in w.queue.tasks if ":cont:" in (t["key"] or "")])


async def test_the_removing_line_is_empty_at_the_end_of_the_tick_that_deleted_the_last_record():
    w = await build(FeedScope.ACCOUNT)
    await w.scope(folders=[w.rd, w.coimbra])
    await w.settle()
    await w.scope(folders=[w.rd])
    await w.svc.tick(SCOPE)
    assert "Coimbra 2024" not in w.names()
    assert (await state_of(w)).removing_roots == ()


async def test_a_waiting_guard_does_not_chain_ticks_but_progress_does(monkeypatch):
    w = await build(FeedScope.ACCOUNT)
    await w.scope(folders=[w.rd, w.coimbra])
    await w.settle()
    before = cont_tasks(w)
    await w.scope(folders=[w.rd])
    for i in range(4):
        w.plant(w.springfield, f"Archive {i}", True)      # a folder change in every tick's feed: the guard stays shut
        await w.svc.tick(SCOPE)
        w.clock.advance(60)
        if i == 0:
            assert "Coimbra notes.txt" in w.names()
    assert cont_tasks(w) == before                        # no back-to-back continuation while the removal only waits
    await w.settle()
    assert "Coimbra notes.txt" not in w.names()
    # progress still continues: a removal bigger than one tick's cascade budget asks for the next tick
    w2 = await build(FeedScope.ACCOUNT)
    for i in range(6):
        w2.plant(w2.coimbra, f"Day {i}.txt")
    await w2.scope(folders=[w2.rd, w2.coimbra])
    await w2.settle()
    base = cont_tasks(w2)
    monkeypatch.setattr(svc_mod, "CASCADE_OPS_PER_TICK", 2)
    await w2.scope(folders=[w2.rd])
    await w2.svc.tick(SCOPE)
    assert cont_tasks(w2) > base


async def test_the_top_level_pass_keeps_its_place_and_kept_records_do_not_spend_the_cascade_budget(monkeypatch):
    w = await build(FeedScope.ACCOUNT)
    for i in range(7):
        w.plant(w.root, f"Loose {i}.txt")
    await w.scope(whole=True)
    await w.settle()
    monkeypatch.setattr(svc_mod, "RECORD_WRITE_BATCH", 2)
    monkeypatch.setattr(svc_mod, "_TOP_LEVEL_PAGES", 1)
    monkeypatch.setattr(svc_mod, "CASCADE_OPS_PER_TICK", 3)
    await w.scope(folders=[w.acme])
    cursors = []
    for _ in range(12):
        await w.svc.tick(SCOPE)
        w.clock.advance(60)
        state = await state_of(w)
        cursors.append(state.top_level_cursor)
        if not state.removing_roots:
            break
    assert any(cursors) and len({c for c in cursors if c}) > 1       # it moved on, tick after tick
    state = await state_of(w)
    assert state.removing_roots == () and set(w.names()) == {"Projects", "Acme", "Acme work.docx"}


async def test_a_parent_map_longer_than_a_tick_of_item_calls_is_read_over_ticks_without_rereading(monkeypatch):
    w = await build(FeedScope.ACCOUNT)
    parent, chain = w.root, []
    for i in range(10):
        parent = w.plant(parent, f"Level {i}", True)
        chain.append(parent)
    await w.scope(folders=[w.rd])
    await w.settle()
    original = w.drive.get_change
    reads = []

    async def counting(handle, item_id):
        reads.append(item_id)
        return await original(handle, item_id)

    w.drive.get_change = counting
    monkeypatch.setattr(svc_mod, "ITEM_CALLS_PER_TICK", 2)
    await w.scope(folders=[w.rd, chain[-1]])
    key = (await w.connections.get("usr-1", "conn-1")).scope_key
    for _ in range(14):
        await w.svc.tick(SCOPE)
        w.clock.advance(60)
        if (await state_of(w)).applied_scope == key:
            break
    assert (await state_of(w)).applied_scope == key
    assert all(reads.count(link) <= 1 for link in chain[:-1])        # no link was read twice


async def test_a_cascade_that_waits_for_an_item_call_reads_no_children(monkeypatch):
    w = await build(FeedScope.ACCOUNT)
    sub = w.plant(w.rd, "Springfield 2", True)
    w.plant(sub, "Spring notes.txt")
    await w.scope(folders=[w.rd])
    await w.settle()
    rec = (await w.repo.get_records(w.handle.scope, [w.rd]))[w.rd]
    await w.repo.upsert_records([schedule(replace(rec, state=RecordState.DELETING, has_card=False, card_hash=None),
                                          w.clock(), first_seen=False)])
    reads = []
    original = w.repo.children

    async def counting(scope, parent_id, after, limit):
        reads.append(parent_id)
        return await original(scope, parent_id, after, limit)

    w.repo.children = counting
    monkeypatch.setattr(svc_mod, "ITEM_CALLS_PER_TICK", 0)
    await w.scope(folders=[sub])
    await w.svc.tick(SCOPE)
    assert w.rd not in reads
    assert "Spring notes.txt" in w.names()


async def test_a_removal_record_a_rollback_left_without_a_due_time_is_due_again():
    w = await build(FeedScope.ACCOUNT)
    await w.scope(folders=[w.rd, w.coimbra])
    await w.settle()
    rec = (await w.repo.get_records(w.handle.scope, [w.coimbra]))[w.coimbra]
    await w.repo.upsert_records([replace(rec, trim_pending=True, due_at=None, priority=None)])
    state = await state_of(w)
    await w.repo.save_sync_state(replace(state, removing_roots=(w.coimbra,)))
    await w.svc.tick(SCOPE)
    after = (await w.repo.get_records(w.handle.scope, [w.coimbra]))[w.coimbra]
    assert not after.trim_pending and (await state_of(w)).removing_roots == ()


async def test_a_new_walk_starts_with_no_blocked_count():
    w = await build(FeedScope.ACCOUNT)
    await w.scope(folders=[w.rd])
    await w.settle()
    state = await state_of(w)
    await w.repo.save_sync_state(replace(state, walk_blocked=5))
    await w.scope(folders=[w.rd, w.coimbra])
    await w.settle()
    assert (await state_of(w)).walk_blocked == 0

"""Second round of fixes after the review of the scope diff: edit-class work deferred under a root being removed never
blocks that removal, a kept root stops the deferral, deferred records ask for no continuation, and an ancestor load
that needs more reads than one tick has calls is resumed over ticks. Synthetic items only."""
from dataclasses import replace

from src.domain.drive_feed_scope import FeedScope
from src.domain.drive_index import DriveIndexRecord, Priority, RecordState, Scope
from src.domain.drive_index_planner import schedule
from src.services import drive_indexing_service as svc_mod

from .test_drive_scope_diff_tick import SCOPE, build, state_of
from .test_drive_scope_fix_batch import cont_tasks


async def make_pending(w, *items):
    for item in items:
        rec = (await w.repo.get_records(w.handle.scope, [item]))[item]
        due = schedule(replace(rec, state=RecordState.PENDING, indexed_generation=None, has_card=False, card_hash=None),
                       w.clock(), first_seen=False)
        assert due.priority == int(Priority.EDIT)
        await w.repo.upsert_records([due])


async def test_edit_class_work_under_a_removed_root_does_not_block_its_removal():
    w = await build(FeedScope.ACCOUNT)
    await w.scope(folders=[w.rd, w.coimbra])
    await w.settle()
    await make_pending(w, w.itinerary, w.coimbra_notes)
    reader = w.svc._extractors["parser"]
    seen = len(reader.requests)
    await w.scope(folders=[w.rd])
    for _ in range(6):
        await w.svc.tick(SCOPE)
        w.clock.advance(60)
    assert "Coimbra 2024" not in w.names() and "Itinerary.txt" not in w.names()
    assert (await state_of(w)).removing_roots == ()
    assert not {r.item_id for r in reader.requests[seen:]} & {w.itinerary, w.coimbra_notes}


async def test_a_file_under_a_kept_root_is_indexed_and_one_under_the_removed_root_outside_it_is_not():
    w = await build(FeedScope.ACCOUNT)
    outside = w.plant(w.rd, "Tom's agenda.txt")
    await w.scope(folders=[w.rd])
    await w.settle()
    await make_pending(w, w.hamburg_notes, outside)
    reader = w.svc._extractors["parser"]
    seen = len(reader.requests)
    await w.scope(folders=[w.hamburg])
    await w.svc.tick(SCOPE)
    read = {r.item_id for r in reader.requests[seen:]}
    assert w.hamburg_notes in read and outside not in read


async def test_deferred_records_under_a_shut_guard_request_no_continuation():
    w = await build(FeedScope.ACCOUNT)
    await w.scope(folders=[w.rd, w.coimbra])
    await w.settle()
    await make_pending(w, w.itinerary)
    before = cont_tasks(w)
    await w.scope(folders=[w.rd])
    for i in range(4):
        w.plant(w.springfield, f"Archive {i}", True)      # the guard stays shut in every tick
        await w.svc.tick(SCOPE)
        w.clock.advance(60)
    assert cont_tasks(w) == before


async def test_an_ancestor_load_longer_than_a_tick_of_item_calls_completes_over_ticks(monkeypatch):
    w = await build(FeedScope.ACCOUNT)
    parent, chain = w.root, []
    for i in range(10):
        parent = w.plant(parent, f"Level {i}", True)
        chain.append(parent)
    await w.scope(folders=[chain[-1]])
    await w.settle()
    # a removal that waits for the load: a folder the scope does not keep, marked deleting
    stray = DriveIndexRecord(user_id="usr-1", connection_id="conn-1", item_id=w.coimbra, parent_id=w.root,
                             name="Coimbra 2024", is_folder=True, scope=Scope.NORMAL, state=RecordState.DELETING,
                             cascade_unstored=True)
    await w.repo.upsert_records([schedule(stray, w.clock(), first_seen=False)])
    await w.repo.delete_records(w.handle.scope, chain[:-1])      # the path records are not stored
    original = w.drive.get_change
    reads = []

    async def counting(handle, item_id):
        reads.append(item_id)
        return await original(handle, item_id)

    w.drive.get_change = counting
    monkeypatch.setattr(svc_mod, "ITEM_CALLS_PER_TICK", 2)
    for _ in range(14):
        await w.svc.tick(SCOPE)
        w.clock.advance(60)
        if "Coimbra 2024" not in w.names():
            break
    assert "Coimbra 2024" not in w.names()                         # the load completed and the removal went on
    assert all(reads.count(link) <= 1 for link in chain[:-1])

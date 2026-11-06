"""Second round of fixes after the review of the scope diff: an ancestor load that needs more reads than one tick has
calls is resumed over ticks. Synthetic items only."""

from src.domain.drive_feed_scope import FeedScope
from src.domain.drive_index import DriveIndexRecord, RecordState, Scope
from src.domain.drive_index_planner import schedule
from src.services import drive_indexing_service as svc_mod

from .test_drive_scope_diff_tick import SCOPE, build


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

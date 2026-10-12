"""A dense folder is not re-embedded on every child change (USER_DRIVE_INDEXING_RFC §5.2, §8.5)."""
from .test_drive_indexing_service import World


async def test_a_child_added_to_a_dense_folder_costs_no_card_embedding():
    w = World(page_size=1000)
    w.drive.reemit_parents = True                # Graph re-emits a folder whose children changed (S0a)
    dense = w.drive.add("logs", folder=True)
    for f in range(600):
        w.drive.add(f"x{f}.log", parent=dense)
    await w.settle()
    assert w.rec(dense).has_card and w.rec(dense).child_count == 600
    w.emb.calls.clear()
    for f in range(600, 610):
        w.drive.add(f"x{f}.log", parent=dense)
    await w.settle()
    assert w.rec(dense).child_count == 610
    assert w.emb.calls == []

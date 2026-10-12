"""Duplicate collapse needs content: every empty file has the same hash (USER_DRIVE_INDEXING_RFC §10.1 step 6)."""
from .test_drive_search_service import World

EMPTY = {"hashes": {"quickXorHash": "AAAAAAAAAAAAAAAAAAAAAAAAAAA="}}


async def test_empty_files_with_the_same_hash_stay_separate_cards():
    w = World()
    w.drive.add("aaa-unrelated-placeholder.txt", size_bytes=0, facets=EMPTY)
    w.drive.add("todo.md", size_bytes=0, facets=EMPTY)
    await w.settle()
    result = await w.search("todo")
    todo = [c for c in result.cards if c.path == "todo.md"]
    assert len(todo) == 1 and todo[0].also_at == ()
    assert all("todo.md" not in c.also_at for c in result.cards)


async def test_non_empty_identical_files_still_collapse():
    w = World()
    same = {"hashes": {"quickXorHash": "QX1"}}
    w.drive.add("lease.pdf", size_bytes=10, facets=same)
    w.drive.add("lease copy.pdf", size_bytes=10, facets=same)
    await w.settle()
    result = await w.search("lease")
    lease = [c for c in result.cards if "lease" in c.path]
    assert len(lease) == 1 and len(lease[0].also_at) == 1

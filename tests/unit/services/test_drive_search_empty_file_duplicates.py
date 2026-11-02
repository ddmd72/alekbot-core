"""Duplicate collapse needs content: every empty file has the same hash (USER_DRIVE_INDEXING_RFC §10.1 step 6)."""
from src.domain.drive_content_identity import ContentIdentity

from .test_drive_search_service import World

EMPTY = ContentIdentity("quickxor", "AAAAAAAAAAAAAAAAAAAAAAAAAAA=", 0)


async def test_empty_files_with_the_same_hash_stay_separate_cards():
    w = World()
    w.drive.add("aaa-unrelated-placeholder.txt", size_bytes=0, content_identity=EMPTY)
    w.drive.add("todo.md", size_bytes=0, content_identity=EMPTY)
    await w.settle()
    result = await w.search("todo")
    # card paths and also_at are canonical (design §1.2 S-7)
    todo = [c for c in result.cards if c.path == "Drive/todo.md"]
    assert len(todo) == 1 and todo[0].also_at == ()
    assert all("Drive/todo.md" not in c.also_at for c in result.cards)


async def test_non_empty_identical_files_still_collapse():
    w = World()
    same = ContentIdentity("quickxor", "QX1", 10)
    w.drive.add("lease.pdf", size_bytes=10, content_identity=same)
    w.drive.add("lease copy.pdf", size_bytes=10, content_identity=same)
    await w.settle()
    result = await w.search("lease")
    lease = [c for c in result.cards if "lease" in c.path]
    assert len(lease) == 1 and len(lease[0].also_at) == 1

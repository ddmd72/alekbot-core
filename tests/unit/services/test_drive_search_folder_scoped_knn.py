"""DriveSearchService: the meaning (kNN) streams of a folder past FOLDER_INMEM_CHUNKS whose walk is complete run over the
folder's readable files only (USER_DRIVE_INDEXING_RFC Δ246), in the search and in passages. Before, a user-wide top-K
kept to the folder lost the folder's chunks whenever closer chunks lay outside it. The repository is the real
in-memory adapter (filter by item id, then nearest); synthetic names only."""
from __future__ import annotations

import asyncio
from typing import List
from unittest.mock import MagicMock

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index import DriveChunk
from src.domain.drive_index_params import KNN_ITEM_IDS_MAX
from src.services import drive_search_service as search_mod
from src.services.drive_search_service import DriveSearchService

from .drive_search_v2_fixture import V2World
from .test_drive_search_excerpts import PassageWorld, toward

QUERY = "zzqx"   # a word no chunk, hint or name holds: the folder's file can only come by meaning
E0 = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


def record_scoped(repo) -> dict:
    """Wraps the scoped kNN of one repository instance: records (limit, item ids) per call, answers with the adapter's
    own semantics. The user-wide kNN calls are recorded too, so a test can show none ran."""
    calls = {"chunks": [], "hints": [], "wide": []}
    chunks_in, hints_in, wide = repo.find_nearest_chunks_in, repo.find_nearest_hints_in, repo.find_nearest_chunks

    async def spy_chunks(user_id, vector, limit, item_ids):
        calls["chunks"].append((limit, tuple(item_ids)))
        return await chunks_in(user_id, vector, limit, item_ids)

    async def spy_hints(user_id, vector, limit, item_ids):
        calls["hints"].append((limit, tuple(item_ids)))
        return await hints_in(user_id, vector, limit, item_ids)

    async def spy_wide(user_id, vector, limit, kind=None):
        calls["wide"].append(limit)
        return await wide(user_id, vector, limit, kind)

    repo.find_nearest_chunks_in, repo.find_nearest_hints_in, repo.find_nearest_chunks = spy_chunks, spy_hints, spy_wide
    return calls


def pin_chunks(w: V2World, item: str, vectors: List[List[float]]) -> None:
    for index, vector in enumerate(vectors):
        chunk, _ = w.repo.chunks[("u", item, 1, index)]
        w.repo.chunks[("u", item, 1, index)] = (chunk, vector)


def log_line(monkeypatch) -> MagicMock:
    log = MagicMock()
    monkeypatch.setattr(search_mod, "logger", log)
    return log


# -- the search -------------------------------------------------------------------------------------------------------

async def test_a_folder_past_the_chunk_bound_finds_its_file_by_meaning_when_closer_chunks_lie_outside(monkeypatch):
    """The user-wide top CHUNK_FETCH is filled by closer chunks outside the folder. Inside, `novel.epub` is near the query
    only by its chunks (its card points away), two other files only by their cards. Every meaning source abstains on
    so small a pool, so the best RELATED_SMALL files by similarity are shown: `novel.epub` leads only if its chunks
    reached the pool, i.e. the kNN ran over the folder's files."""
    monkeypatch.setattr(search_mod, "FOLDER_INMEM_CHUNKS", 2)
    monkeypatch.setattr(search_mod, "CHUNK_FETCH", 3)
    w = V2World()
    w.emb.pinned[QUERY] = E0
    away = [-x for x in E0]
    for n in range(3):   # three chunks at distance 0: a user-wide top-3 holds nothing else
        outside = w.file(f"notes-{n}.txt", kind="txt", chunks=["chapter text"])
        pin_chunks(w, outside, [E0])
    library = w.folder("Library")
    novel = w.file("novel.epub", parent=library, kind="epub", chunks=["one", "two", "three"], card_vector=away)
    pin_chunks(w, novel, [toward(0.5), toward(0.6), toward(0.7)])
    by_card = [w.file(f"book-{n}.txt", parent=library, kind="txt", chunks=["four"], card_vector=toward(1.5))
               for n in range(2)]
    for book in by_card:
        pin_chunks(w, book, [away])
    calls = record_scoped(w.repo)
    log = log_line(monkeypatch)
    result = await w.search(QUERY, folder="Library")
    assert [c.item_id for c in result.cards][0] == novel and result.cards[0].tier == "related"
    assert calls["chunks"] == [(3, (novel, *by_card))] and calls["wide"] == []
    line = log.info.call_args[0][0]
    assert "scope=folder-memory" in line and "content=folder-knn" in line and "chunk=3" in line


async def test_the_hint_knn_runs_over_the_folder_files_too(monkeypatch):
    monkeypatch.setattr(search_mod, "FOLDER_INMEM_CHUNKS", 2)
    w = V2World()
    library = w.folder("Library")
    book = w.file("book-1.epub", parent=library, kind="epub", chunks=["one", "two", "three"],
                  hints={0: [("chapter", "one")]})
    w.file("notes.txt", kind="txt", chunks=["elsewhere"], hints={0: [("chapter", "elsewhere")]})
    calls = record_scoped(w.repo)
    await w.search(QUERY, folder="Library")
    assert calls["hints"] == [(search_mod.HINT_FETCH, (book,))]


async def test_one_scoped_knn_per_member_kind_each_over_the_files_of_that_kind(monkeypatch):
    """A kind family runs one kNN per member kind, as the user-wide streams do; the kind picks the files from the walked
    records, the query has no kind filter."""
    monkeypatch.setattr(search_mod, "FOLDER_INMEM_CHUNKS", 2)
    w = V2World()
    library = w.folder("Library")
    doc = w.file("book-1.docx", parent=library, kind="docx", chunks=["one", "two"])
    pdf = w.file("book-2.pdf", parent=library, kind="pdf", chunks=["three", "four"])
    calls = record_scoped(w.repo)
    await w.search(QUERY, folder="Library", kind="document")
    by_ids = sorted(ids for _, ids in calls["chunks"])
    assert (doc,) in by_ids and (pdf,) in by_ids and all(len(ids) == 1 for ids in by_ids)


# -- passages ---------------------------------------------------------------------------------------------------------

async def test_passages_of_a_folder_past_the_chunk_bound_come_from_its_files_only(monkeypatch):
    """The user-wide kNN (FETCH_MAX) would be filled by nearer chunks outside the folder; the scoped kNN reads only the
    walked readable files and keeps their top CHUNK_FETCH."""
    monkeypatch.setattr(search_mod, "FOLDER_INMEM_CHUNKS", 1)
    monkeypatch.setattr(search_mod, "FETCH_MAX", 2)
    monkeypatch.setattr(search_mod, "CHUNK_FETCH", 2)
    w = PassageWorld()
    for n in range(3):
        w.doc(f"notes-{n}.md", ["The review is held weekly."], [0.0], kind="md")
    library = w.folder("Library")
    novel = w.doc("novel.epub", ["The first review chapter.", "The second review chapter."], [0.5, 0.6],
                  parent=library, kind="epub")
    calls = record_scoped(w.repo)
    log = log_line(monkeypatch)
    result = await w.find(folder="Library")
    assert [f.item_id for f in result.files] == [novel] and result.folder_path == "Library"
    assert calls["chunks"] == [(2, (novel,))] and calls["wide"] == [] and w.repo.knn_limits == []
    assert "scope=folder-scoped" in log.info.call_args[0][0]


async def test_a_folder_of_more_than_thirty_files_is_searched_in_batches_and_keeps_the_global_top(monkeypatch):
    """65 readable files: three batches of at most KNN_ITEM_IDS_MAX ids that cover every file once; the nearest chunk
    lies in the last batch and still leads."""
    monkeypatch.setattr(search_mod, "FOLDER_INMEM_CHUNKS", 1)
    monkeypatch.setattr(search_mod, "CHUNK_FETCH", 5)
    w = PassageWorld()
    library = w.folder("Library")
    count = 2 * KNN_ITEM_IDS_MAX + 5
    books = [w.doc(f"book-{n:02d}.md", [f"Review chapter {n}."], [1.0 - n / count], parent=library, kind="md")
             for n in range(count)]
    calls = record_scoped(w.repo)
    log = log_line(monkeypatch)
    result = await w.find(folder="Library")
    sizes = [len(ids) for _, ids in calls["chunks"]]
    assert sorted(sizes) == [5, KNN_ITEM_IDS_MAX, KNN_ITEM_IDS_MAX]
    assert sorted(i for _, ids in calls["chunks"] for i in ids) == sorted(books)
    assert all(limit == 5 for limit, _ in calls["chunks"]) and calls["wide"] == []
    assert result.files[0].item_id == books[-1]
    assert "hits=5" in log.info.call_args[0][0]


# -- the merge --------------------------------------------------------------------------------------------------------

def _chunk(item: str, index: int) -> DriveChunk:
    return DriveChunk(user_id="u", connection_id="u", item_id=item, generation=1, index=index, text="", char_start=0, char_end=1,
                      kind="md")


async def test_the_merged_batches_equal_the_top_k_over_all_the_files():
    """The union of each batch's top K, sorted by distance and cut at K, is the top K of the whole set: checked against
    one brute-force kNN over the same chunks, with K larger than a batch's file count and the near chunks spread over
    every batch."""
    repo = InMemoryDriveIndexRepository()
    items = [f"book-{n:02d}" for n in range(2 * KNN_ITEM_IDS_MAX + 10)]
    chunks, vectors = [], []
    for n, item in enumerate(items):
        for index in range(3):
            chunks.append(_chunk(item, index))
            vectors.append(toward(((n * 7 + index * 13) % 97) / 10.0))
    await repo.write_chunks(chunks, vectors)
    expected = await repo.find_nearest_chunks("u", E0, 40)   # every chunk belongs to `items`
    for parallel in (1, 16):
        merged = await DriveSearchService._nearest_in(repo.find_nearest_chunks_in, "u", E0, 40, items,
                                                      asyncio.Semaphore(parallel))
        assert [((c.item_id, c.index), d) for c, d in merged] == [((c.item_id, c.index), d) for c, d in expected]


async def test_no_files_make_no_query():
    calls: List[tuple] = []

    async def find(user_id, vector, limit, item_ids):
        calls.append(tuple(item_ids))
        return []

    assert await DriveSearchService._nearest_in(find, "u", E0, 5, [], asyncio.Semaphore(1)) == []
    assert calls == []


async def test_at_most_the_gate_s_batches_run_at_once():
    in_flight, peak = [0], [0]

    async def find(user_id, vector, limit, item_ids):
        in_flight[0] += 1
        peak[0] = max(peak[0], in_flight[0])
        await asyncio.sleep(0)
        in_flight[0] -= 1
        return []

    ids = [f"book-{n:03d}" for n in range(10 * KNN_ITEM_IDS_MAX)]
    await DriveSearchService._nearest_in(find, "u", E0, 5, ids, asyncio.Semaphore(3))
    assert peak[0] == 3


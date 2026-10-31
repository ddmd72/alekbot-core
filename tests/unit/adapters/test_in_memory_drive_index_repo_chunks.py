"""Reference semantics of the chunk half of DriveIndexRepository (USER_DRIVE_INDEXING_RFC §3.3, §5.4, §7.4, §7.5,
§24.6), pinned on the in-memory implementation."""
import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.connection_scope import ConnectionScope
from src.domain.drive_index import DriveChunk, DriveIndexRecord
from tests.unit.services.drive_index_fakes import U_SCOPE


def chunk(item="a", gen=1, index=0, user="u", kind="pdf", terms=(), text=None):
    return DriveChunk(user_id=user, connection_id=user, item_id=item, generation=gen, index=index, text=text or f"{item}/{gen}/{index}",
                      char_start=index * 10, char_end=index * 10 + 10, kind=kind, content_terms=tuple(terms))


@pytest.fixture
def repo():
    return InMemoryDriveIndexRepository()


async def put(repo, *chunks, vector=(1.0, 0.0)):
    await repo.write_chunks(list(chunks), [list(vector)] * len(chunks))


class TestWrite:
    async def test_write_and_replace(self, repo):
        await put(repo, chunk(text="old"))
        await put(repo, chunk(text="new"))
        assert [c.text for c in await repo.get_chunks(U_SCOPE, "a", 1, [0])] == ["new"]

    async def test_vectors_must_match_chunks(self, repo):
        with pytest.raises(ValueError):
            await repo.write_chunks([chunk(), chunk(index=1)], [[1.0, 0.0]])
        assert repo.chunks == {}


class TestDeleteChunksExcept:
    async def test_keeps_one_generation(self, repo):
        await put(repo, chunk(gen=1), chunk(gen=1, index=1), chunk(gen=2), chunk(item="b", gen=1))
        assert await repo.delete_chunks_except(U_SCOPE, "a", 2) == 2
        assert sorted((k[1], k[2]) for k in repo.chunks) == [("a", 2), ("b", 1)]

    async def test_none_deletes_every_generation(self, repo):
        await put(repo, chunk(gen=1), chunk(gen=2), chunk(user="v"))
        assert await repo.delete_chunks_except(U_SCOPE, "a", None) == 2
        assert list(repo.chunks) == [("v", "a", 1, 0)]

    async def test_nothing_to_delete(self, repo):
        assert await repo.delete_chunks_except(U_SCOPE, "a", 1) == 0


class TestFindNearest:
    async def test_nearest_first_with_distance(self, repo):
        await repo.write_chunks([chunk(item="far"), chunk(item="near")], [[0.0, 1.0], [1.0, 0.1]])
        hits = await repo.find_nearest_chunks(U_SCOPE, [1.0, 0.0], 10)
        assert [c.item_id for c, _ in hits] == ["near", "far"]
        assert hits[0][1] < hits[1][1] and hits[1][1] == pytest.approx(1.0)

    async def test_kind_user_and_limit(self, repo):
        await put(repo, chunk(item="a"), chunk(item="b", kind="docx"), chunk(item="c", user="v"), chunk(item="d"))
        assert {c.item_id for c, _ in await repo.find_nearest_chunks(U_SCOPE, [1.0, 0.0], 10, kind="pdf")} == {"a", "d"}
        assert len(await repo.find_nearest_chunks(U_SCOPE, [1.0, 0.0], 1)) == 1

    async def test_not_fenced(self, repo):
        """The service fences (§7.4): a generation no record names is still returned here."""
        await put(repo, chunk(gen=7))
        assert [c.generation for c, _ in await repo.find_nearest_chunks(U_SCOPE, [1.0, 0.0], 5)] == [7]


class TestFindByTerm:
    async def test_matches_whole_terms_in_key_order(self, repo):
        await put(repo, chunk(item="b", terms=("1250.00", "2024")), chunk(item="a", index=1, terms=("1250.00",)),
                  chunk(item="a", index=0, terms=("12500",)), chunk(item="c", user="v", terms=("1250.00",)))
        hits = await repo.find_chunks_by_term(U_SCOPE, "1250.00", 10)
        assert [(c.item_id, c.index) for c in hits] == [("a", 1), ("b", 0)]

    async def test_kind_and_limit(self, repo):
        await put(repo, chunk(item="a", terms=("x99",)), chunk(item="b", kind="docx", terms=("x99",)),
                  chunk(item="c", terms=("x99",)))
        assert [c.item_id for c in await repo.find_chunks_by_term(U_SCOPE, "x99", 10, kind="docx")] == ["b"]
        assert [c.item_id for c in await repo.find_chunks_by_term(U_SCOPE, "x99", 1)] == ["a"]


class TestGetChunks:
    async def test_window_by_index_skipping_missing(self, repo):
        await put(repo, chunk(index=0), chunk(index=1), chunk(index=2), chunk(gen=2, index=1))
        got = await repo.get_chunks(U_SCOPE, "a", 1, [2, -1, 1, 1, 3])
        assert [(c.generation, c.index) for c in got] == [(1, 1), (1, 2)]


class TestReadableChunkTotal:
    async def test_sums_records_with_a_readable_generation(self, repo):
        await repo.upsert_records([
            DriveIndexRecord(user_id="u", connection_id="u", item_id="a", indexed_generation=1, chunk_count=10),
            DriveIndexRecord(user_id="u", connection_id="u", item_id="b", indexed_generation=None, chunk_count=99),   # chunk-cap refusal
            DriveIndexRecord(user_id="u", connection_id="u", item_id="c", indexed_generation=3, chunk_count=5),
            DriveIndexRecord(user_id="v", connection_id="v", item_id="d", indexed_generation=1, chunk_count=7),
        ])
        assert await repo.readable_chunk_total(U_SCOPE) == 15
        assert await repo.readable_chunk_total(ConnectionScope(user_id="nobody", connection_id="nobody")) == 0


class TestPagingAndPurge:
    async def test_chunks_page_orders_by_key_and_resumes(self, repo):
        await put(repo, chunk(item="b"), chunk(item="a", gen=2), chunk(item="a", gen=1, index=1),
                  chunk(item="a", gen=1, index=0), chunk(item="z", user="v"))
        first = await repo.chunks_page(U_SCOPE, None, 3)
        assert [(c.item_id, c.generation, c.index) for c in first] == [("a", 1, 0), ("a", 1, 1), ("a", 2, 0)]
        rest = await repo.chunks_page(U_SCOPE, ("a", 2, 0), 10)
        assert [(c.item_id, c.generation, c.index) for c in rest] == [("b", 1, 0)]

    async def test_index_order_is_numeric(self, repo):
        await put(repo, chunk(index=10), chunk(index=2))
        assert [c.index for c in await repo.chunks_page(U_SCOPE, None, 5)] == [2, 10]

    async def test_delete_user_chunks_in_batches(self, repo):
        await put(repo, chunk(index=0), chunk(index=1), chunk(index=2), chunk(user="v"))
        assert await repo.delete_user_chunks(U_SCOPE, 2) == 2
        assert await repo.delete_user_chunks(U_SCOPE, 2) == 1
        assert await repo.delete_user_chunks(U_SCOPE, 2) == 0
        assert list(repo.chunks) == [("v", "a", 1, 0)]

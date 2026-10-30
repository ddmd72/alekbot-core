"""`find_next_file_with_term` (USER_DRIVE_INDEXING_RFC §24.6, arbiter R2 S-M1): the first chunk holding a term in a
file that sorts after a given item — past EVERY chunk of that item, so each probe names a new file. Pinned on the
in-memory reference, then on the Firestore query: one array_contains, the term page's order, a cursor on item_id
alone, limit 1."""
import pytest
from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore
from google.cloud.firestore_v1.async_query import AsyncQuery

from src.adapters.firestore_drive_index_repo import STREAM_FIELDS, FirestoreDriveIndexRepository
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository

from tests.unit.adapters.firestore_drive_chunk_fakes import CHUNKS, ChunkDb, chunk_snap, make_chunk_repo
from tests.unit.adapters.firestore_drive_fakes import executed
from tests.unit.services.drive_index_fakes import U_SCOPE
from tests.support.g1a3_drive_chunks import chunk


class TestInMemory:
    @pytest.fixture
    def repo(self):
        return InMemoryDriveIndexRepository()

    async def _put(self, repo, *chunks):
        await repo.write_chunks(list(chunks), [[1.0, 0.0]] * len(chunks))

    async def test_skips_every_chunk_of_the_item_it_starts_after(self, repo):
        await self._put(repo, *(chunk(item="a", index=i, terms=("2024",)) for i in range(5)),
                        chunk(item="b", index=3, terms=("2024",)), chunk(item="b", index=1, terms=("2024",)),
                        chunk(item="c", terms=("2024",)))
        nxt = await repo.find_next_file_with_term(U_SCOPE, "2024", "a")
        assert (nxt.item_id, nxt.index) == ("b", 1)   # the first chunk of the next file, in key order
        assert (await repo.find_next_file_with_term(U_SCOPE, "2024", "b")).item_id == "c"
        assert await repo.find_next_file_with_term(U_SCOPE, "2024", "c") is None

    async def test_term_user_and_kind(self, repo):
        await self._put(repo, chunk(item="b", terms=("1999",)), chunk(item="c", user="v", terms=("2024",)),
                        chunk(item="d", kind="docx", terms=("2024",)), chunk(item="e", terms=("2024",)))
        assert (await repo.find_next_file_with_term(U_SCOPE, "2024", "a")).item_id == "d"
        assert (await repo.find_next_file_with_term(U_SCOPE, "2024", "a", kind="pdf")).item_id == "e"
        assert await repo.find_next_file_with_term(U_SCOPE, "2024", "e") is None

    async def test_an_item_id_that_holds_no_chunk_is_a_plain_position(self, repo):
        await self._put(repo, chunk(item="b", terms=("2024",)))
        assert (await repo.find_next_file_with_term(U_SCOPE, "2024", "a")).item_id == "b"
        assert await repo.find_next_file_with_term(U_SCOPE, "2024", "bb") is None


class TestFirestore:
    @pytest.fixture
    def db(self):
        return ChunkDb()

    @pytest.fixture
    def repo(self, db, mock_env_config):
        return make_chunk_repo(db, mock_env_config)

    async def test_one_array_contains_with_a_cursor_on_item_id_limit_one(self, repo, db):
        db.cols[CHUNKS].respond = lambda ops: [chunk_snap(chunk(item="b", terms=("2024",)))]
        found = await repo.find_next_file_with_term(U_SCOPE, "2024", "a")
        assert found.item_id == "b"
        (ops,) = executed(db, CHUNKS)
        assert ops == [("where", "user_id", "==", "u"), ("where", "content_terms", "array_contains", "2024"),
                       ("order_by", "item_id", "ASCENDING"), ("order_by", "generation", "ASCENDING"),
                       ("order_by", "index", "ASCENDING"), ("start_after", {"item_id": "a"}), ("limit", 1),
                       ("select", STREAM_FIELDS)]

    async def test_kind_and_exhausted(self, repo, db):
        assert await repo.find_next_file_with_term(U_SCOPE, "2024", "a", kind="xlsx") is None
        (ops,) = executed(db, CHUNKS)
        assert ops[1] == ("where", "kind", "==", "xlsx")

    async def test_the_sent_cursor_holds_only_the_item_id_and_starts_after_it(self, monkeypatch, mock_env_config):
        """A cursor shorter than the ordering positions past every document sharing its prefix: past all of the
        item's chunks, whatever their generation and index."""
        sent = []

        async def capture(self, *args, **kwargs):
            sent.append(self._to_protobuf())
            return []

        monkeypatch.setattr(AsyncQuery, "get", capture)
        client = firestore.AsyncClient(project="p", credentials=AnonymousCredentials(), database="d")
        mock_env_config.drive_index_records_collection = "test_drive_index_records"
        mock_env_config.drive_sync_state_collection = "test_drive_sync_state"
        mock_env_config.drive_index_chunks_collection = "test_drive_index_chunks"
        repo = FirestoreDriveIndexRepository(client, mock_env_config)
        assert await repo.find_next_file_with_term(U_SCOPE, "2024", "a") is None
        (query,) = sent
        assert [o.field.field_path for o in query.order_by] == ["item_id", "generation", "index"]
        assert [v.string_value for v in query.start_at.values] == ["a"] and not query.start_at.before
        assert query.limit == 1

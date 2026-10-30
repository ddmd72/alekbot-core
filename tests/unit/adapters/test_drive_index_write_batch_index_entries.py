"""Write batches are bounded by request bytes plus index-entry bytes (USER_DRIVE_INDEXING_RFC Δ245): Firestore counts
both toward a commit, and a long document's chunk write was refused on every attempt."""
from datetime import datetime, timezone

from google.cloud.firestore_v1.vector import Vector

from src.adapters.firestore_drive_index_repo import chunk_to_doc, index_entry_bytes, stored_bytes
from src.domain.drive_index import DriveChunk
from src.domain.drive_index_params import WORD_KEYS_PER_CHUNK, WRITE_BATCH_BYTES

from tests.unit.adapters.firestore_drive_chunk_fakes import CHUNKS, ChunkDb, make_chunk_repo

NAME = "col/id"
N_CHUNKS = 300
ENTRY = (sum(len(s) + 1 for s in NAME.split("/")) + 16) + 32   # name size + the fixed 32


class TooBig(Exception):
    pass


class Ref:
    def __init__(self, path):
        self.path = path


class LimitBatch:
    """Records a commit's writes and refuses one whose request + index-entry estimate is over the limit."""

    def __init__(self, db):
        self.db, self.writes = db, []

    def set(self, ref, data, merge=None):
        self.writes.append((ref, data))

    async def commit(self):
        total = sum(stored_bytes(d) + index_entry_bytes(r.path, d) for r, d in self.writes)
        if total > WRITE_BATCH_BYTES:
            raise TooBig("Transaction too big. Decrease transaction size.")
        self.db.commits.append(self.writes)


class LimitDb(ChunkDb):
    def __init__(self):
        super().__init__()
        self.commits = []
        self.cols[CHUNKS].document = lambda doc_id: Ref(f"{CHUNKS}/{doc_id}")

    def batch(self):
        return LimitBatch(self)


def long_document_chunk(index):
    return DriveChunk(user_id="user-1", connection_id="conn-1", item_id="item-1", generation=1, index=index, text="lorem " * 400,
                      char_start=0, char_end=2_400, kind="pdf",
                      word_keys=tuple(f"w:token{n:07d}" for n in range(WORD_KEYS_PER_CHUNK // 2)), keys_version=1)


class TestIndexEntryBytes:
    def test_a_scalar_counts_twice(self):
        assert index_entry_bytes(NAME, {"a": 5}) == 2 * (ENTRY + 2 + stored_bytes(5))
        mixed = {"a": "x", "b": None, "c": True, "d": datetime(2026, 1, 1, tzinfo=timezone.utc)}
        assert index_entry_bytes(NAME, mixed) > 0

    def test_each_array_element_counts_once(self):
        keys = ["w:a", "w:b", "w:c"]
        assert index_entry_bytes(NAME, {"k": keys[:1]}) == ENTRY + 2 + stored_bytes("w:a")
        assert index_entry_bytes(NAME, {"k": keys}) == sum(ENTRY + 2 + stored_bytes(k) for k in keys)
        assert index_entry_bytes(NAME, {"k": []}) == 0

    def test_a_map_is_counted_by_dotted_path(self):
        assert index_entry_bytes(NAME, {"m": {"n": 1}}) == 2 * (ENTRY + 2 + 2 + stored_bytes(1))
        assert index_entry_bytes(NAME, {"m": {"n": 1}}) > index_entry_bytes(NAME, {"n": 1})

    def test_a_vector_counts_nothing(self):
        assert index_entry_bytes(NAME, {"v": Vector([0.5] * 768)}) == 0

    def test_a_longer_document_name_raises_the_estimate(self):
        assert index_entry_bytes("col/" + "i" * 40, {"a": 1}) > index_entry_bytes(NAME, {"a": 1})


class TestBatching:
    async def test_a_long_document_is_written_in_commits_the_store_accepts(self, mock_env_config):
        db = LimitDb()
        chunks = [long_document_chunk(i) for i in range(N_CHUNKS)]
        assert len(chunks[0].word_keys) <= WORD_KEYS_PER_CHUNK
        await make_chunk_repo(db, mock_env_config).write_chunks(chunks, [[0.25] * 768] * len(chunks))
        sizes = [len(c) for c in db.commits]
        assert sum(sizes) == N_CHUNKS
        assert len(sizes) >= 2 and all(n >= 20 for n in sizes[:-1])
        for commit in db.commits:
            assert sum(stored_bytes(d) + index_entry_bytes(r.path, d) for r, d in commit) <= WRITE_BATCH_BYTES

    def test_request_bytes_alone_would_have_overshot(self):
        doc = {**chunk_to_doc(long_document_chunk(0)), "vector": Vector([0.25] * 768)}
        held_by_request = WRITE_BATCH_BYTES // (stored_bytes(doc) + 1_024)   # chunks an old batch held
        assert held_by_request * (stored_bytes(doc) + index_entry_bytes("c/i", doc)) > WRITE_BATCH_BYTES

    async def test_small_writes_stay_in_one_commit(self, mock_env_config):
        db = LimitDb()
        chunks = [DriveChunk(user_id="user-1", connection_id="conn-1", item_id="item-1", generation=1, index=i, text="lorem", char_start=0,
                             char_end=5, kind="pdf", word_keys=("w:token0001",), keys_version=1) for i in range(10)]
        await make_chunk_repo(db, mock_env_config).write_chunks(chunks, [[0.25] * 768] * 10)
        assert [len(c) for c in db.commits] == [10]

    async def test_a_reference_without_a_string_path_still_batches(self, mock_env_config):
        db = ChunkDb()   # MagicMock references: their `path` is not a string
        await make_chunk_repo(db, mock_env_config).write_chunks([long_document_chunk(0)], [[0.25] * 768])
        assert len(db.batches) == 1

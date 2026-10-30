"""The folder in-memory ranking reads only what it ranks with (USER_DRIVE_INDEXING_RFC §10.1 Folder, review D minor).

It ranks chunks by vector distance and by the query's terms; a window reads its chunks by id afterwards. So it never
reads chunk text, reads `content_terms` only when the query has terms, and reads vectors only when there is a query
vector. While the index is refreshing (vectors in another space) and the query has no terms, it reads nothing."""
from src.adapters.firestore_drive_index_repo import CHUNK_VECTOR
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index import DriveChunk

from tests.unit.adapters.firestore_drive_chunk_fakes import ChunkDb, make_chunk_repo

from .drive_index_fakes import U_SCOPE
from .test_drive_search_content_streams import FAR, ContentSpy, index, settle_metadata
from .test_drive_search_service import World


class ReadSpy(ContentSpy):
    def __init__(self) -> None:
        super().__init__()
        self.ranking_reads = []

    async def get_chunks_with_vectors(self, user_id, item_id, generation, indexes, **flags):
        self.ranking_reads.append(flags)
        return await super().get_chunks_with_vectors(user_id, item_id, generation, indexes, **flags)


def world():
    w = World()
    spy = ReadSpy()
    w.repo = spy
    w.indexer._repo = spy
    w.search_svc._repo = spy
    return w


def _vectors_off(w):
    state = w.repo.states["u"]
    w.repo.states["u"] = type(state)(**{**state.__dict__, "applied_space": "another-space/8"})


async def _folder_world(monkeypatch):
    w = world()
    work = w.drive.add("Work", folder=True)
    doc = w.drive.add("scan.pdf", parent=work)
    await settle_metadata(w, monkeypatch)
    return w, doc


async def test_refreshing_without_terms_reads_no_chunk(monkeypatch):
    w, doc = await _folder_world(monkeypatch)
    _vectors_off(w)
    await index(w, doc, ["the boiler warranty lasts five years"], [FAR])
    await w.search("boiler warranty", folder="Work")
    # §25.5.2 (spec change): every v2 word is a concept tested on the chunks' word keys, so a word query always has
    # terms; while refreshing the ranking still reads neither chunk text nor vectors, only the keys (finding the file
    # on real word keys: test_drive_search_v2_ported.test_folder_ranking_while_refreshing_reads_word_keys_but_no_vectors)
    assert w.repo.ranking_reads == [{"text": False, "terms": True, "vectors": False}]


async def test_the_in_memory_repository_leaves_out_what_was_not_asked_for():
    repo = InMemoryDriveIndexRepository()
    await repo.write_chunks([DriveChunk("u", "a", 1, 0, "text 98765", 0, 10, "pdf", ("98765",),
                                        connection_id="u")], [[0.5, 0.5]])
    ((chunk, vector),) = await repo.get_chunks_with_vectors(U_SCOPE, "a", 1, [0], text=False, terms=False, vectors=False)
    assert (chunk.text, chunk.content_terms, vector) == ("", (), [])
    ((chunk, vector),) = await repo.get_chunks_with_vectors(U_SCOPE, "a", 1, [0])
    assert (chunk.text, chunk.content_terms, vector) == ("text 98765", ("98765",), [0.5, 0.5])


async def test_firestore_reads_only_the_asked_fields(mock_env_config):
    db = ChunkDb()
    repo = make_chunk_repo(db, mock_env_config)
    db.stored["u:a:1:0"] = {"user_id": "u", "item_id": "a", "generation": 1, "index": 0, "text": "t",
                            "char_start": 0, "char_end": 1, "kind": "pdf", "content_terms": ["98765"]}
    rows = await repo.get_chunks_with_vectors(U_SCOPE, "a", 1, [0], text=False, terms=True, vectors=False)
    ((_, paths),) = db.get_all_calls
    assert "text" not in paths and CHUNK_VECTOR not in paths and "content_terms" in paths
    assert [(c.index, v) for c, v in rows] == [(0, [])]

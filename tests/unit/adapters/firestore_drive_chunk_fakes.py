"""The recording Firestore client of firestore_drive_fakes, extended for the chunk collection (USER_DRIVE_INDEXING_RFC
§3.3, §24.6): queries also take `start_after` and `sum`, and the records collection answers `sum` too."""
from types import SimpleNamespace

from src.adapters.firestore_drive_index_repo import FirestoreDriveIndexRepository, chunk_to_doc
from src.domain.drive_index import DriveChunk

from tests.unit.adapters.firestore_drive_fakes import RECS, STATES, Collection, Db, Query, snap

CHUNKS = "test_drive_index_chunks"


class ChunkQuery(Query):
    def _then(self, op):
        return ChunkQuery(self.col, self.ops + [op])

    def start_after(self, values):
        return self._then(("start_after", dict(values)))

    def sum(self, field, alias=None):
        return self._then(("sum", field, alias))


class ChunkCollection(Collection):
    def __init__(self, name):
        super().__init__(name)
        self.respond = lambda ops: ([[SimpleNamespace(alias="n", value=0)]] if ops[-1][0] in ("count", "sum")
                                    else [])

    def where(self, *, filter):
        return ChunkQuery(self, []).where(filter=filter)

    def select(self, paths):
        return ChunkQuery(self, []).select(paths)


class ChunkDb(Db):
    def __init__(self):
        super().__init__()
        self.cols = {RECS: ChunkCollection(RECS), STATES: Collection(STATES), CHUNKS: ChunkCollection(CHUNKS)}


def make_chunk_repo(db, env_config):
    env_config.drive_index_records_collection = RECS
    env_config.drive_sync_state_collection = STATES
    env_config.drive_index_chunks_collection = CHUNKS
    return FirestoreDriveIndexRepository(db, env_config)


def chunk(item="a", gen=1, index=0, user="u", kind="pdf", terms=(), text=None):
    return DriveChunk(user_id=user, item_id=item, generation=gen, index=index, text=text or f"{item}/{gen}/{index}",
                      char_start=index * 10, char_end=index * 10 + 10, kind=kind, content_terms=tuple(terms))


def chunk_snap(c, **extra):
    return snap({**chunk_to_doc(c), **extra}, f"{c.user_id}:{c.item_id}:{c.generation}:{c.index}")

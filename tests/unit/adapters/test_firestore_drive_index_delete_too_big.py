"""Every batch delete of the drive index repository survives Firestore's "Transaction too big" (InvalidArgument): a
rejected batch is halved until it fits, a single document that still fails is raised, and any other error is not
split. A delete's index entries count toward the commit size and the delete carries no byte estimate."""
import pytest
from google.api_core.exceptions import InvalidArgument, PermissionDenied

from src.domain.drive_index_params import KEYED_DELETE_BATCH, RECORD_WRITE_BATCH

from tests.unit.adapters.firestore_drive_chunk_fakes import CHUNKS, ChunkDb, chunk, chunk_snap, make_chunk_repo
from tests.unit.adapters.firestore_drive_fakes import RECS, snap
from tests.unit.services.drive_index_fakes import U_SCOPE

LIMIT = 10   # a commit holding more deletes than this is "too big"


class SizedBatch:
    def __init__(self, db):
        self.db = db
        self.refs = []

    def delete(self, ref):
        self.refs.append(ref)

    async def commit(self):
        self.db.attempts.append(len(self.refs))
        if self.db.error is not None:
            raise self.db.error
        if len(self.refs) > self.db.limit:
            raise InvalidArgument("400 Transaction too big. Decrease transaction size.")
        self.db.deleted.extend(r.doc_id for r in self.refs)


@pytest.fixture
def db():
    d = ChunkDb()
    d.deleted, d.attempts, d.limit, d.error = [], [], LIMIT, None
    d.batch = lambda: SizedBatch(d)
    return d


@pytest.fixture
def repo(db, mock_env_config):
    return make_chunk_repo(db, mock_env_config)


async def delete_records(repo, db, n):
    await repo.delete_records(U_SCOPE, [f"i{i}" for i in range(n)])


async def delete_user_records(repo, db, n):
    db.cols[RECS].respond = lambda ops: [snap({}, f"r{i}") for i in range(n)]
    assert await repo.delete_user_records(U_SCOPE, RECORD_WRITE_BATCH) == n


async def delete_user_chunks(repo, db, n):
    db.cols[CHUNKS].respond = lambda ops: [chunk_snap(chunk(index=i)) for i in range(n)]
    assert await repo.delete_user_chunks(U_SCOPE, RECORD_WRITE_BATCH) == n


SITES = [delete_records, delete_user_records, delete_user_chunks]


@pytest.mark.parametrize("site", SITES, ids=lambda f: f.__name__)
async def test_a_too_big_batch_is_halved_until_every_document_is_deleted(repo, db, site):
    n = KEYED_DELETE_BATCH - 1   # one batch for every site, over LIMIT
    assert n > LIMIT
    await site(repo, db, n)
    assert len(db.deleted) == n and len(set(db.deleted)) == n
    assert db.attempts[0] == n and max(db.attempts[1:]) <= n // 2 + 1   # the first commit was rejected, then halves


@pytest.mark.parametrize("site", SITES, ids=lambda f: f.__name__)
async def test_a_batch_that_fits_is_committed_once(repo, db, site):
    await site(repo, db, LIMIT)
    assert db.attempts == [LIMIT] and len(db.deleted) == LIMIT


async def test_a_single_document_that_still_fails_is_raised(repo, db):
    db.limit = 0
    with pytest.raises(InvalidArgument):
        await delete_records(repo, db, 3)
    assert db.attempts[-1] == 1 and db.deleted == []


async def test_another_error_is_not_split(repo, db):
    db.error = PermissionDenied("denied")
    with pytest.raises(PermissionDenied):
        await delete_records(repo, db, 4)
    assert db.attempts == [4]

"""FirestoreSkillRepository over a MagicMock client: the one test left after the rest moved to test_skill_store_kit.py.
It stays only because it is this file's `slow` site in tests/skip_ledger.txt; the kit's
test_delete_skill_lists_the_file_refs_without_reading_them_and_deletes_in_chunks_of_450 asserts the same."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.firestore_skill_repository import FirestoreSkillRepository
from src.config.environment import EnvironmentConfig


@pytest.fixture
def env_config():
    cfg = MagicMock(spec=EnvironmentConfig)
    cfg.skills_collection = "test_skills"
    cfg.skill_drafts_collection = "test_skill_drafts"
    return cfg


@pytest.fixture
def col():
    return MagicMock()


@pytest.fixture
def col_drafts():
    return MagicMock()


@pytest.fixture
def db(col, col_drafts):
    d = MagicMock()

    def _collection(name):
        if name == "test_skill_drafts":
            return col_drafts
        if name == "test_skills":
            return col
        raise AssertionError(f"unexpected collection: {name}")

    d.collection.side_effect = _collection
    return d


@pytest.fixture
def repo(db, env_config):
    return FirestoreSkillRepository(db, env_config)


def _snap(data, exists=True, id=None):
    s = MagicMock()
    s.exists = exists
    s.id = id
    s.to_dict.return_value = data
    return s


def _aiter(items):
    async def gen(*a, **k):
        for i in items:
            yield i
    return gen


@pytest.mark.slow
async def test_delete_skill_removes_files_via_list_documents(repo, db, col):
    batches = []

    def _new_batch():
        b = MagicMock()
        b.commit = AsyncMock()
        batches.append(b)
        return b

    db.batch.side_effect = _new_batch
    doc_ref = MagicMock()
    doc_ref.get = AsyncMock(return_value=_snap({"name": "s"}))
    file_refs = [MagicMock(name=f"file{i}") for i in range(460)]
    files_col = MagicMock()
    files_col.list_documents = MagicMock(side_effect=_aiter(file_refs))
    files_col.get = AsyncMock()
    v1, v2 = MagicMock(), MagicMock()
    versions_col = MagicMock()
    versions_col.get = AsyncMock(return_value=[v1, v2])
    doc_ref.collection.side_effect = lambda name: {"files": files_col, "versions": versions_col}[name]
    col.document.return_value = doc_ref

    assert await repo.delete_skill("u1", "s") is True

    files_col.get.assert_not_called()
    assert [c.args[0] for c in doc_ref.collection.call_args_list] == ["files", "versions"]
    assert len(batches) == 2
    sizes = [b.delete.call_count for b in batches]
    assert sizes == [450, 13] and all(s <= 450 for s in sizes)
    deleted = [c.args[0] for b in batches for c in b.delete.call_args_list]
    assert set(file_refs) <= set(deleted)
    assert v1.reference in deleted and v2.reference in deleted
    # Index doc goes in the final chunk, after everything it points at.
    assert batches[-1].delete.call_args_list[-1].args[0] is doc_ref
    for b in batches:
        b.commit.assert_awaited_once()

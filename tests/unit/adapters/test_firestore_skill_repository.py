from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.firestore_skill_repository import FirestoreSkillRepository
from src.config.environment import EnvironmentConfig
from src.domain.exceptions import SkillCapExceeded
from src.domain.skill import Skill


@pytest.fixture
def env_config():
    cfg = MagicMock(spec=EnvironmentConfig)
    cfg.skills_collection = "test_skills"
    return cfg


@pytest.fixture
def col():
    return MagicMock()


@pytest.fixture
def db(col):
    d = MagicMock()
    d.collection.return_value = col
    return d


@pytest.fixture
def repo(db, env_config):
    return FirestoreSkillRepository(db, env_config)


def _snap(data, exists=True):
    s = MagicMock()
    s.exists = exists
    s.to_dict.return_value = data
    return s


def _doc(name="flight-status", current=2, **extra):
    data = {"user_id": "u1", "account_id": "a1", "name": name,
            "description": "Use when a flight is asked about.", "body": "1. Open.", "current": current}
    data.update(extra)
    return _snap(data)


def test_collection_comes_from_env_config(db, repo):
    db.collection.assert_called_with("test_skills")


def test_env_config_skills_collection_is_prefixed(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    cfg = EnvironmentConfig()
    assert cfg.skills_collection == f"{cfg.firestore_collection_prefix}skills"


class TestListCurrent:
    async def test_queries_by_user_id_equality_and_maps_fields(self, repo, col):
        query = MagicMock()
        query.get = AsyncMock(return_value=[_doc("b-skill", 1), _doc("a-skill", 3)])
        col.where.return_value = query

        skills = await repo.list_current("u1")

        flt = col.where.call_args.kwargs["filter"]
        assert (flt.field_path, flt.op_string, flt.value) == ("user_id", "==", "u1")
        assert [s.name for s in skills] == ["a-skill", "b-skill"]  # sorted by name
        assert skills[0].version == 3 and skills[0].body == "1. Open."

    async def test_corrupt_document_is_skipped(self, repo, col):
        query = MagicMock()
        query.get = AsyncMock(return_value=[_snap({"name": "broken"}), _doc("ok-skill", 1)])
        col.where.return_value = query

        skills = await repo.list_current("u1")

        assert [s.name for s in skills] == ["ok-skill"]


class TestSaveVersion:
    @pytest.fixture(autouse=True)
    def passthrough_transaction(self):
        with patch(
            "src.adapters.firestore_skill_repository.firestore.async_transactional",
            side_effect=lambda fn: fn,
        ):
            yield

    def _setup(self, db, col, existing_doc, user_doc_count=0):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=existing_doc)
        version_ref = MagicMock()
        doc_ref.collection.return_value.document.return_value = version_ref
        col.document.return_value = doc_ref
        query = MagicMock()
        query.get = AsyncMock(return_value=[MagicMock()] * user_doc_count)
        col.where.return_value = query
        txn = MagicMock()
        db.transaction.return_value = txn
        return doc_ref, version_ref, query, txn

    async def test_first_save_writes_v1_and_index(self, repo, db, col):
        doc_ref, version_ref, query, txn = self._setup(db, col, _snap(None, exists=False))
        skill = Skill(name="flight-status", description="Use when x.", body="1. Open.")

        version = await repo.save_version("u1", "a1", skill, cap=20)

        assert version == 1
        col.document.assert_called_with("u1:flight-status")
        doc_ref.collection.assert_called_with("versions")
        doc_ref.collection.return_value.document.assert_called_with("v1")
        query.get.assert_awaited_once_with(transaction=txn)  # cap counted inside the txn
        written = {c.args[0]: c.args[1] for c in txn.set.call_args_list}
        assert written[version_ref]["body"] == "1. Open."
        assert written[doc_ref]["current"] == 1
        assert written[doc_ref]["user_id"] == "u1" and written[doc_ref]["account_id"] == "a1"
        assert written[doc_ref]["name"] == "flight-status"

    async def test_resave_increments_version_without_counting(self, repo, db, col):
        doc_ref, version_ref, query, txn = self._setup(db, col, _doc(current=4), user_doc_count=20)
        skill = Skill(name="flight-status", description="Use when x.", body="new")

        version = await repo.save_version("u1", "a1", skill, cap=20)

        assert version == 5
        doc_ref.collection.return_value.document.assert_called_with("v5")
        query.get.assert_not_awaited()  # an update never hits the cap

    async def test_new_skill_over_cap_raises_and_writes_nothing(self, repo, db, col):
        doc_ref, version_ref, query, txn = self._setup(db, col, _snap(None, exists=False), user_doc_count=20)
        skill = Skill(name="new-one", description="Use when x.", body="b")

        with pytest.raises(SkillCapExceeded):
            await repo.save_version("u1", "a1", skill, cap=20)

        txn.set.assert_not_called()

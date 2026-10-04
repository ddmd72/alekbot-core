from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.api_core.exceptions import AlreadyExists

from src.adapters.firestore_skill_repository import FirestoreSkillRepository
from src.config.environment import EnvironmentConfig
from src.domain.skill import Skill


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


def _snap(data, exists=True):
    s = MagicMock()
    s.exists = exists
    s.to_dict.return_value = data
    return s


def test_env_skill_drafts_collection_is_prefixed(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    cfg = EnvironmentConfig()
    assert cfg.skill_drafts_collection == f"{cfg.firestore_collection_prefix}skill_drafts"


class TestCreateDraft:
    async def test_creates_doc_and_returns_true(self, repo, col_drafts):
        doc_ref = MagicMock()
        doc_ref.create = AsyncMock()
        col_drafts.document.return_value = doc_ref
        skill = Skill(name="flight-status", description="Use when a flight is asked about.", body="1. Open.")

        result = await repo.create_draft("u1", "7f3a", skill)

        assert result is True
        col_drafts.document.assert_called_with("u1:7f3a")
        payload = doc_ref.create.call_args.args[0]
        assert payload["user_id"] == "u1"
        assert payload["name"] == "flight-status"
        assert payload["description"] == "Use when a flight is asked about."
        assert payload["body"] == "1. Open."
        assert "created_at" in payload

    async def test_existing_code_returns_false(self, repo, col_drafts):
        doc_ref = MagicMock()
        doc_ref.create = AsyncMock(side_effect=AlreadyExists("exists"))
        col_drafts.document.return_value = doc_ref
        skill = Skill(name="flight-status", description="Use when a flight is asked about.", body="1. Open.")

        result = await repo.create_draft("u1", "7f3a", skill)

        assert result is False


class TestGetDraft:
    async def test_existing_draft_returns_skill(self, repo, col_drafts):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_snap({
            "user_id": "u1", "name": "flight-status",
            "description": "Use when a flight is asked about.", "body": "1. Open.",
        }))
        col_drafts.document.return_value = doc_ref

        skill = await repo.get_draft("u1", "7f3a")

        col_drafts.document.assert_called_with("u1:7f3a")
        assert skill.name == "flight-status"
        assert skill.body == "1. Open."
        assert skill.version == 0

    async def test_missing_draft_returns_none(self, repo, col_drafts):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_snap(None, exists=False))
        col_drafts.document.return_value = doc_ref

        assert await repo.get_draft("u1", "7f3a") is None

    async def test_corrupt_draft_returns_none_and_logs(self, repo, col_drafts):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_snap({"name": "broken"}))
        col_drafts.document.return_value = doc_ref

        with patch("src.adapters.firestore_skill_repository.logger") as mock_logger:
            result = await repo.get_draft("u1", "7f3a")

        assert result is None
        mock_logger.error.assert_called_once()


class TestSaveVersionConsumesDrafts:
    @pytest.fixture(autouse=True)
    def passthrough_transaction(self):
        with patch(
            "src.adapters.firestore_skill_repository.firestore.async_transactional",
            side_effect=lambda fn: fn,
        ):
            yield

    def _setup_skill_doc(self, db, col, existing_doc, user_doc_count=0):
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

    async def test_consumes_named_drafts_before_any_write(self, repo, db, col, col_drafts):
        order = []
        existing_doc = _snap(None, exists=False)

        doc_ref = MagicMock()

        async def _doc_get(*a, **k):
            order.append("doc_get")
            return existing_doc

        doc_ref.get = _doc_get
        version_ref = MagicMock()
        doc_ref.collection.return_value.document.return_value = version_ref
        col.document.return_value = doc_ref

        query = MagicMock()

        async def _user_query_get(*a, **k):
            order.append("user_query_get")
            return []

        query.get = _user_query_get
        col.where.return_value = query

        draft1, draft2 = MagicMock(), MagicMock()
        drafts_query = MagicMock()

        async def _drafts_get(*a, **k):
            order.append("drafts_get")
            return [draft1, draft2]

        drafts_query.get = _drafts_get
        chained_where = MagicMock()
        chained_where.where.return_value = drafts_query
        col_drafts.where.return_value = chained_where

        txn = MagicMock()

        def _txn_set(*a, **k):
            order.append("txn_set")

        txn.set.side_effect = _txn_set
        db.transaction.return_value = txn

        skill = Skill(name="flight-status", description="Use when x.", body="1. Open.")

        version = await repo.save_version("u1", "a1", skill, cap=20, consume_drafts_named="flight-status")

        assert version == 1
        flt1 = col_drafts.where.call_args.kwargs["filter"]
        assert (flt1.field_path, flt1.op_string, flt1.value) == ("user_id", "==", "u1")
        flt2 = chained_where.where.call_args.kwargs["filter"]
        assert (flt2.field_path, flt2.op_string, flt2.value) == ("name", "==", "flight-status")
        assert order.index("drafts_get") < order.index("txn_set")
        txn.delete.assert_any_call(draft1.reference)
        txn.delete.assert_any_call(draft2.reference)

    async def test_without_consume_arg_no_drafts_query(self, repo, db, col, col_drafts):
        doc_ref, version_ref, query, txn = self._setup_skill_doc(db, col, _snap(None, exists=False))
        skill = Skill(name="flight-status", description="Use when x.", body="1. Open.")

        await repo.save_version("u1", "a1", skill, cap=20)

        col_drafts.where.assert_not_called()
        txn.delete.assert_not_called()


class TestDeleteSkill:
    async def test_deletes_versions_and_index_doc(self, repo, col):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_snap({"name": "flight-status"}, exists=True))
        doc_ref.delete = AsyncMock()
        v1, v2 = MagicMock(), MagicMock()
        v1.reference.delete = AsyncMock()
        v2.reference.delete = AsyncMock()
        versions_query = MagicMock()
        versions_query.get = AsyncMock(return_value=[v1, v2])
        doc_ref.collection.return_value = versions_query
        col.document.return_value = doc_ref

        result = await repo.delete_skill("u1", "flight-status")

        assert result is True
        col.document.assert_called_with("u1:flight-status")
        doc_ref.collection.assert_called_with("versions")
        v1.reference.delete.assert_awaited_once()
        v2.reference.delete.assert_awaited_once()
        doc_ref.delete.assert_awaited_once()

    async def test_missing_index_doc_returns_false_and_deletes_nothing(self, repo, col):
        doc_ref = MagicMock()
        doc_ref.get = AsyncMock(return_value=_snap(None, exists=False))
        doc_ref.delete = AsyncMock()
        doc_ref.collection = MagicMock()
        col.document.return_value = doc_ref

        result = await repo.delete_skill("u1", "flight-status")

        assert result is False
        doc_ref.collection.assert_not_called()
        doc_ref.delete.assert_not_awaited()

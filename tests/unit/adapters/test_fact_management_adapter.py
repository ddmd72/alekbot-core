"""FactManagementAdapter.update_fact / merge_facts: re-embedding and lifecycle flags."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from src.adapters.fact_management_adapter import FactManagementAdapter
from src.domain.entities import FactEntity, FactState, FactType
from src.domain.fact_embedding import fact_embedding_texts
from src.ports.embedding_service import EmbeddingService
from src.ports.fact_write_port import FactWritePort
from src.ports.repository import FactRepository
from src.ports.search_enrichment_port import SearchEnrichmentPort

ACC = "acc"
OLD = [[0.1], [0.2], [0.3]]
NEW = [[1.0], [2.0], [3.0]]


def _fact() -> FactEntity:
    return FactEntity(
        id="fact-12345678", account_id="acc", created_by_user_id="usr", lineage_id="lin",
        text="mother is Maria", type=FactType.EVENT,
        tags=["family", "mother", "consolidated"], metadata={"name": "Maria", "city": "Kyiv"},
        vector=OLD[0], tags_vector=OLD[1], metadata_vector=OLD[2],
    )


@pytest.fixture
def fact():
    return _fact()


@pytest.fixture
def repo(fact):
    r = AsyncMock(spec=FactRepository)
    r.get_fact_by_id.return_value = fact
    return r


@pytest.fixture
def embedding():
    e = AsyncMock(spec=EmbeddingService)
    e.get_embeddings_batch.return_value = NEW
    return e


@pytest.fixture
def adapter(repo, embedding):
    return FactManagementAdapter(
        repo, embedding, AsyncMock(spec=FactWritePort), AsyncMock(spec=SearchEnrichmentPort)
    )


def _written(repo) -> FactEntity:
    repo.update_fact.assert_awaited_once()
    return repo.update_fact.await_args.args[0]


async def test_content_change_reembeds_all_three_vectors(adapter, repo, embedding):
    res = await adapter.update_fact("fact-12345678", {"content": "mother is Maria Petrivna"}, ACC)
    assert res["status"] == "updated"
    embedding.get_embeddings_batch.assert_awaited_once_with(
        fact_embedding_texts(
            "mother is Maria Petrivna",
            ["family", "mother", "consolidated"],
            {"name": "Maria", "city": "Kyiv"},
        ),
        "RETRIEVAL_DOCUMENT",
    )
    w = _written(repo)
    assert (w.vector, w.tags_vector, w.metadata_vector) == (NEW[0], NEW[1], NEW[2])


async def test_tags_change_reembeds(adapter, repo, embedding):
    await adapter.update_fact("fact-12345678", {"tags": ["family"]}, ACC)
    embedding.get_embeddings_batch.assert_awaited_once()
    assert embedding.get_embeddings_batch.await_args.args[0][1] == "family"
    assert _written(repo).tags_vector == NEW[1]


async def test_metadata_is_merged_and_reembedded(adapter, repo, embedding):
    await adapter.update_fact("fact-12345678", {"metadata": {"city": "Lviv", "age": 70}}, ACC)
    merged = {"name": "Maria", "city": "Lviv", "age": 70}
    assert _written(repo).metadata == merged
    embedding.get_embeddings_batch.assert_awaited_once_with(
        fact_embedding_texts("mother is Maria", ["family", "mother", "consolidated"], merged),
        "RETRIEVAL_DOCUMENT",
    )


@pytest.mark.parametrize(
    "state,is_current",
    [("ARCHIVED", True), ("SUPERSEDED", False), ("INVALIDATED", True)],
)
async def test_state_only_update_keeps_vectors_and_sets_lifecycle(
    adapter, repo, embedding, state, is_current
):
    await adapter.update_fact("fact-12345678", {"state": state}, ACC)
    embedding.get_embeddings_batch.assert_not_awaited()
    w = _written(repo)
    assert w.state == FactState(state.lower())
    assert w.is_current is is_current
    assert w.valid_to is not None
    assert (w.vector, w.tags_vector, w.metadata_vector) == (OLD[0], OLD[1], OLD[2])


async def test_same_text_again_does_not_reembed(adapter, repo, embedding):
    await adapter.update_fact("fact-12345678", {"content": "mother is Maria"}, ACC)
    embedding.get_embeddings_batch.assert_not_awaited()
    assert _written(repo).vector == OLD[0]


async def test_embedding_failure_fails_and_writes_nothing(adapter, repo, embedding):
    embedding.get_embeddings_batch.side_effect = RuntimeError("boom")
    res = await adapter.update_fact("fact-12345678", {"content": "new"}, ACC)
    assert res["status"] == "failed"
    repo.update_fact.assert_not_awaited()


async def test_non_dict_metadata_is_ignored(adapter, repo, embedding):
    res = await adapter.update_fact("fact-12345678", {"metadata": "oops"}, ACC)
    assert res["status"] == "updated"
    embedding.get_embeddings_batch.assert_not_awaited()
    assert _written(repo).metadata == {"name": "Maria", "city": "Kyiv"}


async def test_not_found(adapter, repo):
    repo.get_fact_by_id.return_value = None
    res = await adapter.update_fact("missing-id-1", {"content": "x"}, ACC)
    assert res["status"] == "not_found"
    repo.update_fact.assert_not_awaited()


async def test_merge_facts_supersedes_each_source(adapter, repo):
    a, b = _fact(), _fact()
    repo.get_fact_by_id.side_effect = [a, b]
    adapter._fact_write_service.add_facts_batch.return_value = (1, 0, ["new-id"])
    await adapter.merge_facts(
        ["a-1234567", "b-1234567"], "merged",
        {"account_id": ACC, "domain": "biographical", "temporal_class": "permanent", "context_priority": "medium", "tags": ["t"]},
    )
    for f in (a, b):
        assert f.state == FactState.SUPERSEDED
        assert f.is_current is False
        assert f.valid_to is not None
    assert repo.update_fact.await_count >= 2


async def test_foreign_account_update_is_not_found_and_untouched(adapter, repo, embedding):
    res = await adapter.update_fact("fact-12345678", {"content": "hijack"}, "other-account")
    assert res["status"] == "not_found"
    repo.update_fact.assert_not_awaited()
    embedding.get_embeddings_batch.assert_not_awaited()


async def test_merge_skips_foreign_source_and_supersedes_own(adapter, repo):
    own, foreign = _fact(), _fact()
    foreign.account_id = "other-account"
    repo.get_fact_by_id.side_effect = [foreign, own]
    adapter._fact_write_service.add_facts_batch.return_value = (1, 0, ["new-id"])
    await adapter.merge_facts(
        ["f-1234567", "o-1234567"], "merged",
        {"account_id": ACC, "domain": "biographical", "temporal_class": "permanent",
         "context_priority": "medium", "tags": ["t"]},
    )
    assert own.state == FactState.SUPERSEDED
    assert foreign.state == FactState.CURRENT and foreign.is_current is True
    assert foreign.valid_to is None


async def test_state_only_update_keeps_version_and_reported_date(adapter, repo, fact):
    before_reported, before_version = fact.reported_date, fact.version
    await adapter.update_fact("fact-12345678", {"state": "INVALIDATED"}, ACC)
    w = _written(repo)
    assert w.version == before_version
    assert w.reported_date == before_reported


async def test_state_only_update_with_explicit_reported_date_applies_it(adapter, repo, fact):
    await adapter.update_fact(
        "fact-12345678", {"state": "ARCHIVED", "reported_date": "2020-01-02T03:04:05Z"}, ACC
    )
    w = _written(repo)
    assert w.reported_date == datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert w.version == 1


async def test_temporal_class_change_bumps_version_without_reembedding(adapter, repo, embedding):
    await adapter.update_fact("fact-12345678", {"temporal_class": "PERMANENT"}, ACC)
    embedding.get_embeddings_batch.assert_not_awaited()
    assert _written(repo).version == 2


async def test_content_change_bumps_version_and_resets_reported_date(adapter, repo, fact):
    fact.reported_date = datetime.now(timezone.utc) - timedelta(days=30)
    await adapter.update_fact("fact-12345678", {"content": "mother is Maria P."}, ACC)
    w = _written(repo)
    assert w.version == 2
    assert datetime.now(timezone.utc) - w.reported_date < timedelta(minutes=1)

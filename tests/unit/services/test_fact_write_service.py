"""
Unit tests for FactWriteService.

Tests the add_facts_batch pipeline: multi-vector generation, type mapping,
tag augmentation, DFM taxonomy resolution, deduplication, and batch results.
Mocks FactRepository and EmbeddingService (ports).
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.services.fact_write_service import FactWriteService
from src.domain.entities import (
    FactType,
    FactDomain,
    TemporalClass,
    FactState,
    ContextPriority,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_embedding():
    svc = MagicMock()
    svc.get_embedding = AsyncMock(return_value=[0.1] * 768)
    svc.get_embeddings_batch = AsyncMock(return_value=[[0.1] * 768, [0.2] * 768, [0.3] * 768])
    return svc


@pytest.fixture
def mock_repo():
    repo = MagicMock()
    repo.add_fact_if_unique = AsyncMock(return_value=(True, None))
    repo.add_fact = AsyncMock(return_value="new-fact-id")
    return repo


@pytest.fixture
def service(mock_repo, mock_embedding):
    return FactWriteService(mock_repo, mock_embedding)


# ---------------------------------------------------------------------------
# Empty batch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_batch_returns_zero(service):
    saved, skipped, ids = await service.add_facts_batch("acc-1", "usr-1", [])
    assert saved == 0
    assert skipped == 0
    assert ids == []


# ---------------------------------------------------------------------------
# Single fact — basic save
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_fact_saved(service, mock_repo):
    facts = [{"text": "User owns a cat", "tags": ["pet"], "type": "event"}]

    saved, skipped, ids = await service.add_facts_batch("acc-1", "usr-1", facts)

    assert saved == 1
    assert skipped == 0
    mock_repo.add_fact_if_unique.assert_awaited_once()


# ---------------------------------------------------------------------------
# Type mapping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_type_mapping_state(service, mock_repo):
    facts = [{"text": "User weighs 75kg", "tags": [], "type": "state"}]

    await service.add_facts_batch("acc-1", "usr-1", facts)

    call_args = mock_repo.add_fact_if_unique.call_args
    fact_entity = call_args.args[0] if call_args.args else call_args.kwargs.get("fact")
    assert fact_entity.type == FactType.STATE


@pytest.mark.asyncio
async def test_type_mapping_principle(service, mock_repo):
    facts = [{"text": "User values honesty above all", "tags": [], "type": "principle"}]

    await service.add_facts_batch("acc-1", "usr-1", facts)

    call_args = mock_repo.add_fact_if_unique.call_args
    fact_entity = call_args.args[0] if call_args.args else call_args.kwargs.get("fact")
    assert fact_entity.type == FactType.PRINCIPLE


@pytest.mark.asyncio
async def test_type_mapping_unknown_defaults_to_event(service, mock_repo):
    facts = [{"text": "Some unknown fact", "tags": [], "type": "unknown_type"}]

    await service.add_facts_batch("acc-1", "usr-1", facts)

    call_args = mock_repo.add_fact_if_unique.call_args
    fact_entity = call_args.args[0] if call_args.args else call_args.kwargs.get("fact")
    assert fact_entity.type == FactType.EVENT


@pytest.mark.asyncio
async def test_type_mapping_alert(service, mock_repo):
    facts = [{"text": "User has peanut allergy", "tags": [], "type": "alert"}]

    await service.add_facts_batch("acc-1", "usr-1", facts)

    call_args = mock_repo.add_fact_if_unique.call_args
    fact_entity = call_args.args[0] if call_args.args else call_args.kwargs.get("fact")
    assert fact_entity.type == FactType.ALERT


@pytest.mark.asyncio
async def test_type_mapping_system(service, mock_repo):
    facts = [{"text": "Always respond in Ukrainian", "tags": [], "type": "system"}]

    await service.add_facts_batch("acc-1", "usr-1", facts)

    call_args = mock_repo.add_fact_if_unique.call_args
    fact_entity = call_args.args[0] if call_args.args else call_args.kwargs.get("fact")
    assert fact_entity.type == FactType.SYSTEM


# ---------------------------------------------------------------------------
# Tag augmentation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_consolidated_tag_added(service, mock_repo):
    facts = [{"text": "Fact text", "tags": ["pet"], "type": "event"}]

    await service.add_facts_batch("acc-1", "usr-1", facts)

    call_args = mock_repo.add_fact_if_unique.call_args
    fact_entity = call_args.args[0] if call_args.args else call_args.kwargs.get("fact")
    assert "consolidated" in fact_entity.tags


@pytest.mark.asyncio
async def test_consolidated_tag_not_duplicated(service, mock_repo):
    facts = [{"text": "Fact text", "tags": ["consolidated", "pet"], "type": "event"}]

    await service.add_facts_batch("acc-1", "usr-1", facts)

    call_args = mock_repo.add_fact_if_unique.call_args
    fact_entity = call_args.args[0] if call_args.args else call_args.kwargs.get("fact")
    assert fact_entity.tags.count("consolidated") == 1


@pytest.mark.asyncio
async def test_anchor_tag_added_for_principles(service, mock_repo):
    facts = [{"text": "User is a night owl", "tags": [], "type": "principle"}]

    await service.add_facts_batch("acc-1", "usr-1", facts)

    call_args = mock_repo.add_fact_if_unique.call_args
    fact_entity = call_args.args[0] if call_args.args else call_args.kwargs.get("fact")
    assert "anchor" in fact_entity.tags


@pytest.mark.asyncio
async def test_anchor_tag_not_added_for_events(service, mock_repo):
    facts = [{"text": "User went to Paris", "tags": [], "type": "event"}]

    await service.add_facts_batch("acc-1", "usr-1", facts)

    call_args = mock_repo.add_fact_if_unique.call_args
    fact_entity = call_args.args[0] if call_args.args else call_args.kwargs.get("fact")
    assert "anchor" not in fact_entity.tags


# ---------------------------------------------------------------------------
# DFM taxonomy fields
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dfm_domain_resolved(service, mock_repo):
    facts = [{"text": "Fact", "tags": [], "type": "event", "domain": "biographical"}]

    await service.add_facts_batch("acc-1", "usr-1", facts)

    call_args = mock_repo.add_fact_if_unique.call_args
    fact_entity = call_args.args[0] if call_args.args else call_args.kwargs.get("fact")
    assert fact_entity.domain == FactDomain.BIOGRAPHICAL


@pytest.mark.asyncio
async def test_dfm_temporal_class_resolved(service, mock_repo):
    facts = [{"text": "Fact", "tags": [], "type": "state", "temporal_class": "dynamic"}]

    await service.add_facts_batch("acc-1", "usr-1", facts)

    call_args = mock_repo.add_fact_if_unique.call_args
    fact_entity = call_args.args[0] if call_args.args else call_args.kwargs.get("fact")
    assert fact_entity.temporal_class == TemporalClass.DYNAMIC


@pytest.mark.asyncio
async def test_dfm_defaults_when_absent(service, mock_repo):
    facts = [{"text": "Fact without DFM", "tags": [], "type": "event"}]

    await service.add_facts_batch("acc-1", "usr-1", facts)

    call_args = mock_repo.add_fact_if_unique.call_args
    fact_entity = call_args.args[0] if call_args.args else call_args.kwargs.get("fact")
    assert fact_entity.domain is None
    assert fact_entity.temporal_class is None
    assert fact_entity.state == FactState.CURRENT
    assert fact_entity.context_priority == ContextPriority.MEDIUM


# ---------------------------------------------------------------------------
# Multi-vector generation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_three_embeddings_generated_per_fact(service, mock_embedding):
    facts = [{"text": "User likes coffee", "tags": ["drink", "coffee"], "type": "state"}]

    await service.add_facts_batch("acc-1", "usr-1", facts)

    # 1 batch call with 3 texts per fact (text, tags_text, metadata_text)
    mock_embedding.get_embeddings_batch.assert_awaited_once()
    texts_arg = mock_embedding.get_embeddings_batch.call_args.args[0]
    assert len(texts_arg) == 3


@pytest.mark.asyncio
async def test_embeddings_use_correct_tasks(service, mock_embedding):
    facts = [{"text": "Test text", "tags": ["tag1"], "type": "event"}]

    await service.add_facts_batch("acc-1", "usr-1", facts)

    # All 3 vectors use RETRIEVAL_DOCUMENT (consistent with RETRIEVAL_QUERY at search time)
    task_arg = mock_embedding.get_embeddings_batch.call_args.args[1]
    assert task_arg == "RETRIEVAL_DOCUMENT"


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_duplicate_fact_skipped(service, mock_repo):
    mock_repo.add_fact_if_unique = AsyncMock(return_value=(False, "existing-id"))
    facts = [{"text": "Duplicate fact", "tags": [], "type": "event"}]

    saved, skipped, ids = await service.add_facts_batch("acc-1", "usr-1", facts)

    assert saved == 0
    assert skipped == 1


@pytest.mark.asyncio
async def test_batch_mixed_saved_and_skipped(service, mock_repo):
    mock_repo.add_fact_if_unique = AsyncMock(side_effect=[
        (True, None),    # first fact saved
        (False, "dup"),  # second fact skipped
        (True, None),    # third fact saved
    ])
    facts = [
        {"text": "Fact 1", "tags": [], "type": "event"},
        {"text": "Fact 2 (dup)", "tags": [], "type": "event"},
        {"text": "Fact 3", "tags": [], "type": "state"},
    ]

    saved, skipped, ids = await service.add_facts_batch("acc-1", "usr-1", facts)

    assert saved == 2
    assert skipped == 1


# ---------------------------------------------------------------------------
# skip_deduplication flag
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_skip_deduplication_uses_add_fact(service, mock_repo):
    facts = [{"text": "Direct fact", "tags": [], "type": "event"}]

    saved, skipped, ids = await service.add_facts_batch(
        "acc-1", "usr-1", facts, skip_deduplication=True
    )

    assert saved == 1
    assert skipped == 0
    mock_repo.add_fact.assert_awaited_once()
    mock_repo.add_fact_if_unique.assert_not_awaited()


# ---------------------------------------------------------------------------
# account_id and user_id propagation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_account_id_and_user_id_set_on_entity(service, mock_repo):
    facts = [{"text": "Attributed fact", "tags": [], "type": "event"}]

    await service.add_facts_batch("account-xyz", "user-abc", facts)

    call_args = mock_repo.add_fact_if_unique.call_args
    fact_entity = call_args.args[0] if call_args.args else call_args.kwargs.get("fact")
    assert fact_entity.account_id == "account-xyz"
    assert fact_entity.created_by_user_id == "user-abc"


# ---------------------------------------------------------------------------
# content vs text key
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_content_key_used_when_text_absent(service, mock_repo):
    facts = [{"content": "Fact via content key", "tags": [], "type": "event"}]

    saved, skipped, ids = await service.add_facts_batch("acc-1", "usr-1", facts)

    assert saved == 1
    call_args = mock_repo.add_fact_if_unique.call_args
    fact_entity = call_args.args[0] if call_args.args else call_args.kwargs.get("fact")
    assert fact_entity.text == "Fact via content key"

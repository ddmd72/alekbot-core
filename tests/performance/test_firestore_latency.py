"""
Performance tests for Firestore search operations.

Run manually (real Firestore):
  APP_ENV=development pytest tests/performance/test_firestore_latency.py -v -m performance

Notes:
- These tests hit real Firestore and will be slower than unit tests.
- Ensure GOOGLE_CLOUD_PROJECT and credentials are configured locally.
"""

import time
import pytest
from google.cloud import firestore

from src.config.settings import load_settings
from src.adapters.firestore_repo import FirestoreFactRepository
from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
from src.agents.memory_search_agent import MemorySearchAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentIntent


import os
TEST_ACCOUNT_ID = os.getenv("TEST_ACCOUNT_ID", "test-account-id")
TEST_QUERY = "машина авто модель car"


def _make_repo(config):
    env_config = config["ENVIRONMENT_CONFIG"]
    if env_config.use_emulator:
        db_client = firestore.AsyncClient(project="emulator-project")
    else:
        db_client = firestore.AsyncClient(project=config["GOOGLE_CLOUD_PROJECT"])
    embedding_service = GeminiEmbeddingAdapter(api_key=config["GEMINI_API_KEY"])
    repo = FirestoreFactRepository(db_client, env_config, embedding_service=embedding_service)
    return repo, embedding_service


@pytest.mark.performance
@pytest.mark.asyncio
async def test_memory_search_firestore_latency():
    """Measure MemorySearchAgent latency (embedding + Firestore vector search)."""
    config = load_settings()
    repo, embedding_service = _make_repo(config)
    await repo.initialize()

    agent = MemorySearchAgent(
        config=AgentConfig(
            agent_id="memory_search_perf",
            agent_type="memory_search",
            timeout_ms=10000,
            capabilities=["performance_test"]
        ),
        repository=repo,
        embedding_service=embedding_service,
        account_id=TEST_ACCOUNT_ID
    )

    message = AgentMessage.create(
        sender="performance_test",
        recipient=agent.agent_id,
        intent=AgentIntent.QUERY,
        payload={"query": TEST_QUERY},
        context={}
    )

    start = time.perf_counter()
    response = await agent.execute(message)
    total_ms = (time.perf_counter() - start) * 1000

    assert response.status.value == "success"
    assert "total_duration_ms" in response.metadata
    assert "search_duration_ms" in response.metadata
    assert "embedding_duration_ms" in response.metadata

    print(
        f"MemorySearchAgent latency: total={total_ms:.2f}ms, "
        f"embed={response.metadata['embedding_duration_ms']}ms, "
        f"firestore={response.metadata['search_duration_ms']}ms, "
        f"results={response.metadata['result_count']}"
    )


@pytest.mark.performance
@pytest.mark.asyncio
async def test_biographical_context_cache_latency():
    """Measure cached biographical context read latency (Firestore)."""
    config = load_settings()
    repo, _ = _make_repo(config)
    await repo.initialize()

    start = time.perf_counter()
    facts = await repo.get_biographical_context_cached(TEST_ACCOUNT_ID, limit=100)
    total_ms = (time.perf_counter() - start) * 1000

    assert isinstance(facts, list)

    print(
        f"Biographical context cache latency: total={total_ms:.2f}ms, "
        f"facts={len(facts)}"
    )
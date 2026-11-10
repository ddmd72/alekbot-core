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


import os
TEST_ACCOUNT_ID = os.getenv("TEST_ACCOUNT_ID", "test-account-id")


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
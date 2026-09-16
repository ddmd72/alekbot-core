"""
Integration test for FirestoreCompanionCacheRepository's account_id contract.

Same FirestoreCapturingStub pattern as test_firestore_companion_memory_contracts.py.
"""
from unittest.mock import MagicMock

import pytest

from src.adapters.firestore_companion_cache_repository import (
    FirestoreCompanionCacheRepository,
)
from tests.contracts.adapter_contracts import FIRESTORE_COMPANION_CACHE_SAVE_INCLUDES_ACCOUNT_ID
from tests.integration.adapters.conftest import FirestoreCapturingStub


def _env_config_stub():
    env = MagicMock()
    env.companion_context_cache_collection = "test_companion_context_cache"
    return env


@pytest.mark.asyncio
async def test_save_summary_always_includes_account_id():
    stub = FirestoreCapturingStub()
    repo = FirestoreCompanionCacheRepository(stub.build_db(), _env_config_stub())

    await repo.save_summary("slack:C1", "acc1", "New summary")

    assert len(stub.doc_set_calls) == 1
    FIRESTORE_COMPANION_CACHE_SAVE_INCLUDES_ACCOUNT_ID.validate(
        "firestore_companion_cache", stub.doc_set_calls[0]
    )

"""
Integration tests for FirestoreCompanionMemoryRepository contracts.

Same CapturingStub + ContractRule pattern as
test_firestore_indexed_email_contracts.py (R18.2).
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.adapters.firestore_companion_memory_repository import (
    FirestoreCompanionMemoryRepository,
)
from src.domain.companion import CompanionRecord
from tests.contracts.adapter_contracts import (
    FIRESTORE_COMPANION_FIND_NEAREST_FILTERS_ACCOUNT_ID,
    FIRESTORE_COMPANION_FIND_NEAREST_FILTERS_SESSION_ID,
    FIRESTORE_COMPANION_SAVE_BATCH_INCLUDES_ACCOUNT_ID,
)
from tests.integration.adapters.conftest import FirestoreCapturingStub


def _env_config_stub():
    env = MagicMock()
    env.companion_records_collection = "test_companion_records"
    return env


def _record(session_id="slack:C1", account_id="acc1") -> CompanionRecord:
    return CompanionRecord(
        session_id=session_id,
        account_id=account_id,
        created_by_user_id="user1",
        text="Recurring subjunctive error",
        vector=[0.1, 0.2, 0.3],
        tags=["grammar"],
        domain="grammar_error",
        created_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_find_nearest_always_filters_session_id():
    """SECURITY contract: cross-session leak risk if session_id filter is missing."""
    stub = FirestoreCapturingStub()
    repo = FirestoreCompanionMemoryRepository(stub.build_db(), _env_config_stub())

    await repo.find_nearest(session_id="slack:C1", account_id="acc1", query_vector=[0.1] * 8, limit=5)

    assert len(stub.find_nearest_calls) == 1
    FIRESTORE_COMPANION_FIND_NEAREST_FILTERS_SESSION_ID.validate(
        "firestore_companion_memory", stub.find_nearest_calls[0]
    )


@pytest.mark.asyncio
async def test_find_nearest_always_filters_account_id():
    """SECURITY contract: cross-account leak risk if account_id filter is missing."""
    stub = FirestoreCapturingStub()
    repo = FirestoreCompanionMemoryRepository(stub.build_db(), _env_config_stub())

    await repo.find_nearest(session_id="slack:C1", account_id="acc1", query_vector=[0.1] * 8, limit=5)

    FIRESTORE_COMPANION_FIND_NEAREST_FILTERS_ACCOUNT_ID.validate(
        "firestore_companion_memory", stub.find_nearest_calls[0]
    )


@pytest.mark.asyncio
async def test_save_batch_always_includes_account_id():
    """BILLING contract: RFC §6 — account_id indexed on every record from day one."""
    stub = FirestoreCapturingStub()
    repo = FirestoreCompanionMemoryRepository(stub.build_db(), _env_config_stub())

    await repo.save_batch([_record(account_id="acc1"), _record(account_id="acc2")])

    assert len(stub.batch_set_calls) == 2
    for call in stub.batch_set_calls:
        FIRESTORE_COMPANION_SAVE_BATCH_INCLUDES_ACCOUNT_ID.validate(
            "firestore_companion_memory", call
        )


@pytest.mark.asyncio
async def test_save_batch_skips_empty_input():
    stub = FirestoreCapturingStub()
    repo = FirestoreCompanionMemoryRepository(stub.build_db(), _env_config_stub())

    written = await repo.save_batch([])

    assert written == 0
    assert stub.batch_set_calls == []
    assert stub.batch_commits == 0


@pytest.mark.asyncio
async def test_find_nearest_returns_companion_records():
    stub = FirestoreCapturingStub()
    repo = FirestoreCompanionMemoryRepository(stub.build_db(), _env_config_stub())

    results = await repo.find_nearest(session_id="slack:C1", account_id="acc1", query_vector=[0.1] * 8, limit=5)

    assert results == []  # stub returns no docs by default; shape-only test


@pytest.mark.asyncio
async def test_find_nearest_applies_distance_threshold():
    """Without a floor, find_nearest returns `limit` docs regardless of similarity —
    Phase A final-review follow-up, closed here now that the assembler (Task 4)
    is a real caller."""
    stub = FirestoreCapturingStub()
    repo = FirestoreCompanionMemoryRepository(stub.build_db(), _env_config_stub())

    await repo.find_nearest(session_id="slack:C1", account_id="acc1", query_vector=[0.1] * 8, limit=5)

    assert stub.find_nearest_calls[0]["kwargs"]["distance_threshold"] == 0.4

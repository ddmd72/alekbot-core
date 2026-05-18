"""
Integration tests for FirestoreIndexedEmailRepository contracts.

Second non-LLM application of the CapturingStub + ContractRule pattern (R18.2).
Demonstrates the shape works for chained-query SDKs (Firestore) in addition to
HTTP boundaries (Gmail) and LLM SDK boundaries.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.adapters.firestore_indexed_email_repo import FirestoreIndexedEmailRepository
from src.domain.email import IndexedEmail
from tests.contracts.adapter_contracts import (
    FIRESTORE_EMAIL_FIND_NEAREST_FILTERS_USER_AND_STATE,
    FIRESTORE_EMAIL_SAVE_BATCH_COMPOSITE_DOC_ID,
)
from tests.integration.adapters.conftest import FirestoreCapturingStub


def _env_config_stub():
    """Minimal env_config double — only the two collection-name properties matter."""
    env = MagicMock()
    env.domain_email_facts_collection = "test_domain_email_facts_v1"
    env.email_indexing_state_collection = "test_email_indexing_state"
    return env


def _email(user_id="user1", email_id="em1") -> IndexedEmail:
    return IndexedEmail(
        email_id=email_id,
        user_id=user_id,
        account_id="acc1",
        source="gmail",
        text="some fact",
        vector=[0.1, 0.2, 0.3],
        tags=["finance"],
        category="receipt",
        metadata={"subject": "x"},
        subject="x",
        from_address="a@b.com",
        email_date=datetime(2026, 1, 15, tzinfo=timezone.utc),
        indexed_at=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_find_nearest_always_filters_user_id_and_state():
    """SECURITY contract: every find_nearest query must filter by user_id AND state.

    Missing user_id → cross-tenant leak. Missing state → archived emails shown.
    """
    stub = FirestoreCapturingStub()
    repo = FirestoreIndexedEmailRepository(stub.build_db(), _env_config_stub())

    await repo.find_nearest(
        user_id="user42",
        vectors={"vector": [0.1] * 8, "tags_vector": [0.2] * 8},
        limit=5,
    )

    # Two vectors → two find_nearest calls. Both must carry both filters.
    assert len(stub.find_nearest_calls) == 2
    for call in stub.find_nearest_calls:
        FIRESTORE_EMAIL_FIND_NEAREST_FILTERS_USER_AND_STATE.validate(
            "firestore_indexed_email", call
        )


@pytest.mark.asyncio
async def test_save_batch_doc_id_is_user_email_composite():
    """IDEMPOTENCY + ISOLATION contract: every batch.set uses '{user_id}_{email_id}' doc id."""
    stub = FirestoreCapturingStub()
    repo = FirestoreIndexedEmailRepository(stub.build_db(), _env_config_stub())

    emails = [
        _email(user_id="userA", email_id="msg1"),
        _email(user_id="userA", email_id="msg2"),
        _email(user_id="userB", email_id="msg1"),  # same provider id, different user
    ]
    await repo.save_batch(emails)

    assert len(stub.batch_set_calls) == 3
    for written, source_email in zip(stub.batch_set_calls, emails):
        payload = {
            "doc_id": written["doc_id"],
            "user_id": source_email.user_id,
            "email_id": source_email.email_id,
        }
        FIRESTORE_EMAIL_SAVE_BATCH_COMPOSITE_DOC_ID.validate(
            "firestore_indexed_email", payload
        )

    # Negative sanity: same-provider-id-different-user must yield distinct doc IDs.
    doc_ids = {c["doc_id"] for c in stub.batch_set_calls}
    assert "userA_msg1" in doc_ids
    assert "userB_msg1" in doc_ids
    assert len(doc_ids) == 3, "composite ID must keep cross-user records distinct"


@pytest.mark.asyncio
async def test_save_batch_skips_empty_input():
    """Sanity: empty input → zero Firestore writes (no spurious batch.commit)."""
    stub = FirestoreCapturingStub()
    repo = FirestoreIndexedEmailRepository(stub.build_db(), _env_config_stub())

    written = await repo.save_batch([])

    assert written == 0
    assert stub.batch_set_calls == []
    assert stub.batch_commits == 0

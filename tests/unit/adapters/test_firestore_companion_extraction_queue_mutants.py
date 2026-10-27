"""FirestoreCompanionExtractionQueue on the evaluating fake: the stored extraction count and the per-session scoping of
the size and recovery queries (the mutation check's survivors). Assertions are on stored documents and returned values."""
import time
from unittest.mock import MagicMock

import pytest

from src.adapters.firestore_companion_extraction_queue import FirestoreCompanionExtractionQueue
from src.domain.companion_extraction import CompanionExtractionBatch
from src.domain.consolidation import BatchStatus
from tests.support.firestore_eval import EvalDb

QUEUE = "companion_queue"


@pytest.fixture
def db():
    return EvalDb()


@pytest.fixture
def queue(db):
    env = MagicMock()
    env.companion_extraction_queue_collection = QUEUE
    return FirestoreCompanionExtractionQueue(db, env)


def _batch(batch_id, session_id, status=BatchStatus.PENDING, messages=1):
    return CompanionExtractionBatch(
        batch_id=batch_id, session_id=session_id, account_id="a1", companion_type="tutor", created_by_user_id="u1",
        messages=[{"role": "user", "parts": [{"text": "hola"}]}] * messages, status=status, created_at=time.time(),
    )


async def test_a_status_update_stores_the_number_of_records_extracted(queue, db):
    await queue.enqueue_batch(_batch("b1", "u1:C1"))

    await queue.update_batch_status("b1", BatchStatus.RETRY_PENDING, error="model timeout", records_extracted=4)

    assert db.collection(QUEUE).docs["b1"]["records_extracted"] == 4
    [pending] = await queue.get_pending_batches("u1:C1")
    assert pending.records_extracted == 4


async def test_the_queue_size_counts_only_the_session_s_own_messages(queue):
    await queue.enqueue_batch(_batch("b1", "u1:C1", messages=2))
    await queue.enqueue_batch(_batch("b2", "u1:C2", messages=5))

    assert await queue.get_queue_size("u1:C1") == 2


async def test_recovery_resets_only_the_session_s_own_failed_batches(queue, db):
    await queue.enqueue_batch(_batch("b1", "u1:C1", status=BatchStatus.FAILED))
    await queue.enqueue_batch(_batch("b2", "u1:C2", status=BatchStatus.FAILED))

    reset = await queue.reset_recoverable_batches("u1:C1")

    assert reset == 1
    docs = db.collection(QUEUE).docs
    assert (docs["b1"]["status"], docs["b2"]["status"]) == (BatchStatus.RETRY_PENDING.value, BatchStatus.FAILED.value)

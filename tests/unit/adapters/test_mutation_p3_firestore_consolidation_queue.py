"""Mutation survivor of `src/adapters/firestore_consolidation_queue.py` (p3 · g6): `get_queue_size` counts only the
asked user's messages (tenancy filter)."""
from types import SimpleNamespace

from src.adapters.firestore_consolidation_queue import FirestoreConsolidationQueue
from src.domain.consolidation import ConsolidationBatch
from tests.support.firestore_eval import EvalDb

QUEUE = "eval_consolidation_queue"


async def _put(queue, batch_id, user_id, n):
    await queue.enqueue_batch(ConsolidationBatch(
        batch_id=batch_id, user_id=user_id, session_id=f"{user_id}:C1",
        messages=[{"role": "user", "text": f"m{i}"} for i in range(n)], created_at=1.0))


async def test_queue_size_counts_only_this_users_messages():
    queue = FirestoreConsolidationQueue(EvalDb(), SimpleNamespace(consolidation_queue_collection=QUEUE))
    await _put(queue, "b1", "usr-1", 2)
    await _put(queue, "b2", "usr-1", 1)
    await _put(queue, "b3", "usr-2", 5)
    assert await queue.get_queue_size("usr-1") == 3
    assert await queue.get_queue_size("usr-2") == 5
    assert await queue.get_queue_size("usr-3") == 0

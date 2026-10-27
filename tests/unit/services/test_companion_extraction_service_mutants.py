"""CompanionExtractionService over the real queue adapter on the evaluating fake: the "more batches left" answer, and the
answer when the queue store fails (the mutation check's survivors). The caller re-enqueues the session's work on True,
so a failure must answer False."""
import time
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

from google.api_core.exceptions import ServiceUnavailable

from src.adapters.firestore_companion_extraction_queue import FirestoreCompanionExtractionQueue
from src.domain.companion_extraction import CompanionExtractionBatch
from src.ports.companion_cache_repository import CompanionCacheRepository
from src.ports.companion_extractor_port import CompanionExtractorPort
from src.ports.companion_memory_repository import CompanionMemoryRepository
from src.ports.embedding_service import EmbeddingService
from src.services.companion_extraction_service import CompanionExtractionService
from tests.support.firestore_eval import EvalDb

QUEUE = "companion_queue"
SESSION = "u1:C1"


class NothingToKeep(CompanionExtractorPort):
    """An extractor run that finds nothing worth keeping (no records, no summary)."""

    async def extract(self, companion_type: str, account_id: str, created_by_user_id: str,
                      messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {"records": [], "summary": ""}


class StoreDownQueue(FirestoreCompanionExtractionQueue):
    """The queue whose pending-batch query fails, as a Firestore outage does."""

    async def get_pending_batches(self, session_id: str, limit: int = 10) -> List[CompanionExtractionBatch]:
        raise ServiceUnavailable("firestore unavailable")


def _env():
    env = MagicMock()
    env.companion_extraction_queue_collection = QUEUE
    return env


def _service(queue):
    return CompanionExtractionService(
        queue=queue, companion_repo=AsyncMock(spec=CompanionMemoryRepository),
        cache_repo=AsyncMock(spec=CompanionCacheRepository), embedding_service=AsyncMock(spec=EmbeddingService),
        extractor=NothingToKeep(),
    )


async def _enqueue(queue, batch_id, created_at):
    await queue.enqueue_batch(CompanionExtractionBatch(
        batch_id=batch_id, session_id=SESSION, account_id="a1", companion_type="tutor", created_by_user_id="u1",
        messages=[{"role": "user", "parts": [{"text": "hola"}]}], created_at=created_at,
    ))


async def test_one_batch_processed_of_two_answers_that_more_are_left():
    db = EvalDb()
    queue = FirestoreCompanionExtractionQueue(db, _env())
    now = time.time()
    await _enqueue(queue, "b1", now - 2)
    await _enqueue(queue, "b2", now - 1)

    has_more = await _service(queue).process_session_batches(SESSION, max_batches=1)

    assert has_more is True
    assert sorted(db.collection(QUEUE).docs) == ["b2"]


async def test_a_failing_queue_store_answers_no_more_work():
    db = EvalDb()
    queue = StoreDownQueue(db, _env())
    await _enqueue(queue, "b1", time.time())

    assert await _service(queue).process_session_batches(SESSION) is False

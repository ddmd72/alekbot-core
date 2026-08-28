from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.companion import CompanionRecord
from src.domain.companion_extraction import CompanionExtractionBatch
from src.domain.consolidation import BatchStatus
from src.ports.companion_extraction_queue import CompanionExtractionQueue
from src.ports.companion_extractor_port import CompanionExtractorPort
from src.ports.companion_cache_repository import CompanionCacheRepository
from src.ports.companion_memory_repository import CompanionMemoryRepository
from src.ports.embedding_service import EmbeddingService
from src.services.companion_extraction_service import CompanionExtractionService

_SESSION_ID = "slack:C123"


def _make_batch(batch_id: str = "b1") -> CompanionExtractionBatch:
    return CompanionExtractionBatch(
        batch_id=batch_id, session_id=_SESSION_ID, account_id="acc-1",
        companion_type="tutor", created_by_user_id="user-1",
        messages=[{"role": "user", "parts": [{"text": "hola"}]}],
    )


@pytest.fixture
def queue():
    q = AsyncMock(spec=CompanionExtractionQueue)
    q.reset_recoverable_batches.return_value = 0
    q.get_pending_batches.return_value = []
    q.update_batch_status.return_value = None
    q.delete_batch.return_value = None
    q.increment_attempts.return_value = 1
    return q


@pytest.fixture
def companion_repo():
    r = AsyncMock(spec=CompanionMemoryRepository)
    r.save_batch.return_value = 1
    return r


@pytest.fixture
def cache_repo():
    return AsyncMock(spec=CompanionCacheRepository)


@pytest.fixture
def embedding_service():
    e = AsyncMock(spec=EmbeddingService)
    e.get_embeddings_batch.return_value = [[0.1, 0.2]]
    return e


@pytest.fixture
def extractor():
    x = AsyncMock(spec=CompanionExtractorPort)
    x.extract.return_value = {
        "records": [{"text": "Confuses subjunctive", "domain": "grammar_error", "tags": []}],
        "summary": "Covered subjunctive mood.",
    }
    return x


@pytest.fixture
def service(queue, companion_repo, cache_repo, embedding_service, extractor):
    return CompanionExtractionService(
        queue=queue, companion_repo=companion_repo, cache_repo=cache_repo,
        embedding_service=embedding_service, extractor=extractor,
    )


async def test_no_pending_batches_returns_false(service, queue):
    queue.get_pending_batches.return_value = []
    has_more = await service.process_session_batches(_SESSION_ID)
    assert has_more is False


async def test_successful_batch_writes_records_and_summary_then_deletes(
    service, queue, companion_repo, cache_repo, embedding_service, extractor,
):
    batch = _make_batch()
    queue.get_pending_batches.side_effect = [[batch], []]

    has_more = await service.process_session_batches(_SESSION_ID, max_batches=1)

    assert has_more is False
    extractor.extract.assert_called_once_with(
        companion_type="tutor", account_id="acc-1", created_by_user_id="user-1",
        messages=batch.messages,
    )
    embedding_service.get_embeddings_batch.assert_called_once_with(["Confuses subjunctive"])
    saved = companion_repo.save_batch.call_args[0][0]
    assert isinstance(saved[0], CompanionRecord)
    assert saved[0].session_id == _SESSION_ID
    assert saved[0].domain == "grammar_error"
    cache_repo.save_summary.assert_called_once_with(_SESSION_ID, "acc-1", "Covered subjunctive mood.")
    queue.delete_batch.assert_called_once_with(batch.batch_id)


async def test_extractor_failure_increments_attempts_and_stops(service, queue, extractor):
    batch = _make_batch()
    queue.get_pending_batches.return_value = [batch]
    extractor.extract.side_effect = Exception("LLM error")

    has_more = await service.process_session_batches(_SESSION_ID, max_batches=1)

    queue.increment_attempts.assert_called_once_with(batch.batch_id)
    queue.delete_batch.assert_not_called()


async def test_third_failed_attempt_marks_batch_failed(service, queue, extractor):
    batch = _make_batch()
    queue.get_pending_batches.return_value = [batch]
    queue.increment_attempts.return_value = 3
    extractor.extract.side_effect = Exception("LLM error")

    await service.process_session_batches(_SESSION_ID, max_batches=1)

    queue.update_batch_status.assert_any_call(batch.batch_id, BatchStatus.FAILED, error="LLM error")


async def test_find_stuck_sessions_delegates_to_queue(service, queue):
    queue.get_stuck_session_ids.return_value = [_SESSION_ID]
    result = await service.find_stuck_sessions()
    assert result == [_SESSION_ID]

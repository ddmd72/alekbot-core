"""The failure that exhausts a companion batch's last allowed revival logs EXTRACTION_GAVE_UP."""
import logging
from unittest.mock import AsyncMock

import pytest

from src.domain.companion_extraction import CompanionExtractionBatch
from src.ports.companion_cache_repository import CompanionCacheRepository
from src.ports.companion_extraction_queue import CompanionExtractionQueue
from src.ports.companion_extractor_port import CompanionExtractorPort
from src.ports.companion_memory_repository import CompanionMemoryRepository
from src.ports.embedding_service import EmbeddingService
from src.services.companion_extraction_service import CompanionExtractionService

SESSION = "u1:C0CHAN"


def _service(batch):
    queue = AsyncMock(spec=CompanionExtractionQueue)
    queue.reset_recoverable_batches.return_value = 0
    queue.get_pending_batches.side_effect = [[batch], []]
    queue.increment_attempts.return_value = 3
    extractor = AsyncMock(spec=CompanionExtractorPort)
    extractor.extract.side_effect = Exception("extractor unavailable")
    return CompanionExtractionService(
        queue=queue, companion_repo=AsyncMock(spec=CompanionMemoryRepository),
        cache_repo=AsyncMock(spec=CompanionCacheRepository),
        embedding_service=AsyncMock(spec=EmbeddingService), extractor=extractor,
    )


def _batch(revivals):
    return CompanionExtractionBatch(
        batch_id="b1", session_id=SESSION, account_id="acc-1", companion_type="tutor",
        created_by_user_id="u1", messages=[{"role": "user"}], revivals=revivals,
    )


@pytest.fixture(autouse=True)
def _capture(caplog):
    caplog.set_level(logging.DEBUG)


def _gave_up(caplog):
    return [r for r in caplog.records if "EXTRACTION_GAVE_UP" in r.getMessage()]


async def test_the_last_allowed_round_failing_logs_gave_up(caplog):
    await _service(_batch(revivals=3)).process_session_batches(SESSION)

    lines = _gave_up(caplog)
    assert len(lines) == 1 and lines[0].levelno == logging.ERROR
    assert "revivals=3" in lines[0].getMessage() and "messages=1" in lines[0].getMessage()


async def test_an_earlier_round_failing_logs_no_gave_up(caplog):
    await _service(_batch(revivals=1)).process_session_batches(SESSION)

    assert _gave_up(caplog) == []
    assert any("EXTRACTION_FAILED" in r.getMessage() for r in caplog.records)

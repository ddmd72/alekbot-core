"""AgentCoordinator.prefetch_file_ref: an answer with neither content nor note leaves the params as they were."""
from unittest.mock import AsyncMock

from src.domain.document_open import Prefetched
from src.infrastructure.agent_coordinator import AgentCoordinator


async def test_an_empty_prefetch_answer_adds_neither_content_nor_note():
    resolver = AsyncMock(return_value=Prefetched())
    coord = AgentCoordinator(file_ref_resolver=resolver)
    params = {"file_ref": "scan_0012.pdf", "query": "summarise"}
    await coord.prefetch_file_ref("some_agent", params, {"user_id": "u1", "account_id": "a1"},
                                  deadline_at=1_900_000_000.0)
    assert params == {"file_ref": "scan_0012.pdf", "query": "summarise"}

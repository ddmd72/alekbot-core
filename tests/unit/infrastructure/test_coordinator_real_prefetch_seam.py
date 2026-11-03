"""Seam: the coordinator calls the REAL FileConversionService.prefetch (keyword-only `deadline_at`) on both the worker
path and the SYNC path; a call-shape mismatch used to end in the 'could not be opened' note. Invented items only."""
import logging

from src.infrastructure.agent_coordinator import AgentCoordinator
from tests.unit.services.test_file_conversion_open import DEADLINE, world  # noqa: F401  (fixture)

CTX = {"user_id": "ann", "account_id": "ann"}


def _real_coordinator(world) -> AgentCoordinator:
    return AgentCoordinator(file_ref_resolver=world.service.prefetch)


async def test_worker_prefetch_delivers_file_content_without_an_error_line(world, caplog):
    params = {"file_ref": "invoice_painting_es.pdf"}
    with caplog.at_level(logging.ERROR):
        await _real_coordinator(world).prefetch_file_ref("files", params, CTX, deadline_at=DEADLINE)
    assert "Total due: 680,00 EUR" in params["file_content"]
    assert "file_note" not in params
    assert "Failed to resolve file_ref" not in caplog.text


async def test_sync_path_delivers_file_content_without_an_error_line(world, caplog):
    coord = _real_coordinator(world)
    seen = []

    async def route(message):
        seen.append(message)
        return None

    coord.route_message = route
    with caplog.at_level(logging.ERROR):
        await coord._execute_sync("files", "files", "read it",
                                  {**CTX, "params": {"file_ref": "invoice_painting_es.pdf"}})
    (message,) = seen
    assert "Total due: 680,00 EUR" in message.payload["file_content"]
    assert "file_note" not in message.payload
    assert "Failed to resolve file_ref" not in caplog.text

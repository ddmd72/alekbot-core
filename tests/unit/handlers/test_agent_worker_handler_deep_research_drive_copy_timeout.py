"""The drive copy is bounded in time (review MINOR, 2026-10-14): a slow Graph sequence must not hold the
delivery task open after the report's link was sent. On the timeout nothing more is posted."""
import asyncio
from unittest.mock import AsyncMock

import src.handlers.agent_worker_handler as worker_module
from src.domain.deep_research_drive_copy import DEEP_RESEARCH_REPORT_CONTEXT_KEY
from src.services.deep_research_drive_copy_service import DeepResearchDriveCopyService

from tests.unit.handlers.test_agent_worker_handler_deep_research_drive_copy import (
    _handler, _html_item, _html_payload, _success,
)


async def test_a_copy_slower_than_the_bound_posts_nothing_and_delivery_succeeds(monkeypatch):
    monkeypatch.setattr(worker_module, "DEEP_RESEARCH_COPY_TIMEOUT_S", 0.05)
    copy = AsyncMock(spec=DeepResearchDriveCopyService)

    async def slow(*_args, **_kwargs):
        await asyncio.sleep(5)
        return "📁 never shown"

    copy.copy_report.side_effect = slow
    handler, coordinator, notification, _ = _handler(copy=copy)
    coordinator.route_message.return_value = _success([_html_item()])

    result = await asyncio.wait_for(
        handler.handle_task(_html_payload(**{DEEP_RESEARCH_REPORT_CONTEXT_KEY: "q"})), timeout=2)

    assert result["status"] == "success"
    notification.notify_document_link.assert_awaited_once()
    notification.notify_raw.assert_not_called()


def test_the_bound_is_a_minute():
    assert worker_module.DEEP_RESEARCH_COPY_TIMEOUT_S == 60.0

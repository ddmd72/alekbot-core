"""Mutation-survivor kills for AgentWorkerHandler: the two best-effort notices log their failure."""
from unittest.mock import AsyncMock, patch

import src.handlers.agent_worker_handler as worker_module
from src.domain.agent import AgentResponse
from src.domain.deep_research_drive_copy import DEEP_RESEARCH_REPORT_CONTEXT_KEY
from src.infrastructure.agent_manifest import Intent
from src.services.deep_research_drive_copy_service import DeepResearchDriveCopyService

from tests.unit.handlers.test_agent_worker_handler import _make_handler, _payload
from tests.unit.handlers.test_agent_worker_handler_deep_research_drive_copy import (
    _handler, _html_item, _html_payload, _success,
)


async def test_a_failing_drive_copy_is_logged_and_does_not_fail_the_delivery():
    copy = AsyncMock(spec=DeepResearchDriveCopyService)
    copy.copy_report.side_effect = RuntimeError("drive down")
    handler, coordinator, _, _ = _handler(copy=copy)
    coordinator.route_message.return_value = _success([_html_item()])

    with patch.object(worker_module, "logger") as log:
        result = await handler.handle_task(
            _html_payload(**{DEEP_RESEARCH_REPORT_CONTEXT_KEY: "q"}))

    assert result["status"] == "success"
    assert any("copy failed" in str(c.args[0]) and "drive down" in str(c.args[-1])
               for c in log.error.call_args_list)


async def test_a_failing_errand_notice_is_logged_and_the_failed_result_still_returns():
    handler, coordinator, notification, _ = _make_handler()
    coordinator.route_message.return_value = AgentResponse.failure(
        task_id="t", agent_id="a", error="no answer")
    notification.notify.side_effect = RuntimeError("slack down")

    with patch.object(worker_module, "logger") as log:
        result = await handler.handle_task(_payload(Intent.SEND_TO_CHAT))

    assert result["status"] == "failed"
    assert any("Errand failure notice failed" in str(c.args[0]) and "slack down" in str(c.args[0])
               for c in log.error.call_args_list)
